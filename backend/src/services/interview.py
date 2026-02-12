"""Interview chat service — plan-then-execute architecture.

Turn 1: Sonnet generates a QuestionPlan from seed data + KB search.
Turns 2+: Haiku processes one answer per turn (single-shot, no history).
Curveball: deviation triggers KB re-fetch + Sonnet re-plan.
"""

import asyncio
import logging
from enum import Enum
from typing import Any, AsyncGenerator, Optional

from src.agents.interview_executor import execute_turn
from src.agents.interview_planner import _FIELD_ENUM_REGISTRY, generate_plan, replan
from src.config.circuit_breaker import CircuitOpenError
from src.models.interview_plan import QuestionPlan
from src.models.requirements import (
    InterviewProgress,
    RoutingProtocol,
    UseCases,
    WorkloadResilience,
)
from src.services.plan_cache import plan_cache
from src.storage import get_store
from src.utils.sse import circuit_breaker_error_message, sse_error, sse_event, with_heartbeats
from src.utils.validation import sanitize_requirements, sanitize_text

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State conversion: QuestionPlan → InterviewProgress (frontend contract)
# ---------------------------------------------------------------------------


def _parse_use_cases(use_case: str | None) -> list[UseCases]:
    """Parse comma-separated use case string into enum list."""
    if not use_case:
        return []
    result: list[UseCases] = []
    for raw in use_case.split(","):
        raw = raw.strip()
        try:
            result.append(UseCases(raw))
        except ValueError:
            continue
    return result


def _safe_enum(enum_cls: type[Enum], value: Any) -> Any:
    """Try to coerce a value to an enum, return None if it doesn't match."""
    if value is None:
        return None
    try:
        return enum_cls(value)
    except (ValueError, KeyError):
        logger.warning("Invalid %s value %r — dropping", enum_cls.__name__, value)
        return None


# Derived from the planner's single registry — only include enum-typed fields
_ENUM_FIELDS: dict[str, type[Enum]] = {
    fp: enum_cls for fp, (enum_cls, etype) in _FIELD_ENUM_REGISTRY.items()
    if etype == "enum"
}


def _plan_to_progress(plan: QuestionPlan, message: str) -> InterviewProgress:
    """Convert plan state to InterviewProgress for the frontend."""
    fields = plan.populated_fields

    # Validate enum fields — revert invalid values so the question is re-asked
    for field_path, enum_cls in _ENUM_FIELDS.items():
        raw = fields.get(field_path)
        if raw is not None and _safe_enum(enum_cls, raw) is None:
            plan.revert_answer(field_path)

    # Extract use_case_fields (dotted paths like "sd-wan.role" → use_case_fields["role"])
    use_case_fields: dict = {}
    for entry in plan.entries:
        if entry.status not in ("answered", "auto_filled"):
            continue
        if "." in entry.field_path:
            # Use-case-specific: "sd-wan.role" → key by field name
            _, field_name = entry.field_path.split(".", 1)
            use_case_fields[field_name] = entry.answered_value
    # Also include auto-filled use-case fields
    for path, value in plan.auto_filled.items():
        if "." in path:
            _, field_name = path.split(".", 1)
            use_case_fields[field_name] = value

    progress = InterviewProgress(
        response_message=message,
        use_cases=fields.get("use_cases"),
        cloud_routing_protocol=_safe_enum(RoutingProtocol, fields.get("cloud_routing_protocol")),
        resilience=_safe_enum(WorkloadResilience, fields.get("resilience")),
        bandwidth=fields.get("bandwidth"),
        user_info=fields.get("user_info"),
        compliance=fields.get("compliance"),
        solution_description=fields.get("solution_description"),
        use_case_fields=use_case_fields,
        complete=plan.blocking_complete() and plan.pending_count() == 0,
        missing_fields=plan.all_missing_field_paths(),
    )
    progress.validate_and_correct_completion()
    return progress


def _extract_gathered_fields(progress: InterviewProgress) -> dict:
    """Extract all non-null gathered fields as a JSON-safe dict."""
    data = progress.model_dump(exclude_none=True, mode="json")
    exclude_keys = {"response_message", "complete", "missing_fields", "use_case_fields"}
    gathered = {k: v for k, v in data.items() if k not in exclude_keys}

    for k, v in data.get("use_case_fields", {}).items():
        if v not in (None, "", []):
            gathered[k] = v
    return gathered


# ---------------------------------------------------------------------------
# SSE event generation
# ---------------------------------------------------------------------------


async def _interview_chat_events(
    message: str,
    tenant_id: str = "default",
    project_id: str = "default",
    requirements: Optional[dict] = None,
    populated_fields: Optional[dict] = None,
    use_case: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """Produce raw SSE events for a single interview turn."""
    message = sanitize_text(message, field_name="message")
    seed = sanitize_requirements(requirements) if requirements else None
    use_cases = _parse_use_cases(use_case)

    session_id = f"interview-{tenant_id}-{project_id}"

    try:
        plan = plan_cache.get(session_id)

        if plan is None:
            # === PLANNING PHASE (Turn 1) ===
            seed_data = seed or {}
            # Pre-populate use_cases and bandwidth from seed
            if use_case:
                seed_data["use_cases"] = [uc.value for uc in use_cases]

            plan, initial_message = await asyncio.to_thread(
                generate_plan, seed_data, use_cases, populated_fields, tenant_id,
            )
            plan_cache.save(session_id, plan)
            progress = _plan_to_progress(plan, initial_message)
        else:
            # === EXECUTION PHASE (Turns 2+) ===
            plan, turn_response = await asyncio.to_thread(
                execute_turn, plan, message, tenant_id,
            )

            if turn_response.deviation_detected and turn_response.deviation_reason:
                # Curveball — re-plan
                plan, replan_message = await asyncio.to_thread(
                    replan, plan, turn_response.deviation_reason, use_cases, tenant_id,
                )
                # Combine: acknowledge + re-plan message
                combined_msg = turn_response.response_message
                if replan_message:
                    combined_msg = f"{turn_response.response_message}\n\n{replan_message}"
                progress = _plan_to_progress(plan, combined_msg)
            else:
                progress = _plan_to_progress(plan, turn_response.response_message)

            plan_cache.save(session_id, plan)

        # Build SSE payload
        evt: dict = {
            "content": progress.response_message,
            "complete": progress.complete,
            "missing_fields": progress.missing_fields,
            "gathered_fields": _extract_gathered_fields(progress),
        }

        if progress.use_case_fields:
            evt["use_case_fields"] = progress.use_case_fields

        # Include input hint for the next question so the frontend can
        # render selectable options (enums) or typed inputs (int/float).
        next_q = plan.current_question()
        if next_q:
            hint: dict[str, Any] = {
                "field_path": next_q.field_path,
                "type": next_q.expected_type,
            }
            if next_q.valid_values:
                hint["options"] = next_q.valid_values
            evt["input_hint"] = hint

        if progress.complete:
            req_doc = progress.to_interview_output()
            evt["requirements"] = req_doc.model_dump()
            try:
                get_store().save_step(tenant_id, project_id, "requirements", req_doc.model_dump())
            except Exception:
                logger.warning("Failed to persist requirements", exc_info=True)

        yield sse_event("message", evt)
        yield sse_event("done", {"status": "ok"})

    except CircuitOpenError as exc:
        logger.warning("Interview blocked by circuit breaker (retry_after=%ds)", exc.retry_after or 30)
        yield sse_error(circuit_breaker_error_message(exc.retry_after))
    except Exception:
        logger.exception("Interview chat failed")
        yield sse_error("Internal server error")


async def interview_chat_stream(
    message: str,
    tenant_id: str = "default",
    project_id: str = "default",
    requirements: Optional[dict] = None,
    populated_fields: Optional[dict] = None,
    use_case: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """Stream interview agent response via SSE.

    Yields SSE-formatted strings with heartbeat keep-alives every 15 s.
    """
    source = _interview_chat_events(
        message=message,
        tenant_id=tenant_id,
        project_id=project_id,
        requirements=requirements,
        populated_fields=populated_fields,
        use_case=use_case,
    )
    async for event in with_heartbeats(source, label="interview"):
        yield event
