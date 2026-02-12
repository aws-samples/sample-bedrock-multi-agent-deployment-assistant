"""Shared design processing pipeline.

Extracted from design.py and design_worker.py to eliminate duplication.
Used by both the Lambda SQS handler and the local async worker.
"""

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime

from src.models.design import (
    DesignOption,
    DesignRecommendation,
    DesignTaskStatus,
)
from src.models.requirements import InterviewOutput
from src.storage import get_store
from src.tools.template_discovery import discover_templates

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------


def build_agent_prompt(
    requirements: InterviewOutput,
    feedback: str | None = None,
    previous_options: list[DesignOption] | None = None,
) -> str:
    """Build the user message for the design agent."""
    req_json = requirements.model_dump_json(indent=2)
    parts = [f"## RequirementsDocument\n```json\n{req_json}\n```"]

    if feedback:
        parts.append(f"\n## User Feedback on Previous Designs\n{feedback}")
    if previous_options:
        prev_json = json.dumps(
            [opt.model_dump() for opt in previous_options], indent=2,
        )
        parts.append(
            f"\n## Previous Design Options (for reference)\n```json\n{prev_json}\n```"
        )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Result extraction
# ---------------------------------------------------------------------------


def extract_recommendation(
    result, all_prefixes: list[str],
) -> DesignRecommendation:
    """Extract DesignRecommendation from the agent result."""
    structured = getattr(result, "structured_output", None)
    if structured is not None and hasattr(structured, "model_dump"):
        rec = (
            structured
            if isinstance(structured, DesignRecommendation)
            else DesignRecommendation.model_validate(structured.model_dump())
        )
    else:
        raw = (
            result.message["content"][0]["text"]
            if hasattr(result, "message")
            else str(result)
        )
        rec = DesignRecommendation.model_validate_json(raw)

    rec.available_templates = all_prefixes
    return rec


# ---------------------------------------------------------------------------
# Template discovery
# ---------------------------------------------------------------------------


def discover_template_summary(requirements: InterviewOutput) -> tuple[str, list[str]]:
    """Discover templates and build a summary string for the agent prompt."""
    uc_values = [uc.value if hasattr(uc, "value") else str(uc) for uc in requirements.use_cases]
    templates_by_uc = discover_templates(uc_values)

    all_prefixes: list[str] = []
    summary_parts: list[str] = []
    for uc, templates in templates_by_uc.items():
        for t in templates:
            all_prefixes.append(t.s3_prefix)
            summary_parts.append(f"{t.use_case}/{t.deployment_type}")

    summary = ", ".join(summary_parts) if summary_parts else "None"
    return summary, all_prefixes


# ---------------------------------------------------------------------------
# Core processing pipeline
# ---------------------------------------------------------------------------


def process_design_task(
    body: dict,
    notify_fn: Callable[[str, str, str, str], None] | None = None,
) -> None:
    """Process a single design generation task.

    This is the shared pipeline used by both the Lambda SQS handler
    and the local async worker.

    Args:
        body: Task message body with task_id, tenant_id, project_id,
              requirements, feedback, previous_options.
        notify_fn: Optional callback(tenant_id, project_id, task_id, status)
                   for post-completion notifications (e.g. WebSocket).
    """
    from src.agents.design import design_agent

    task_id = body["task_id"]
    tenant_id = body["tenant_id"]
    project_id = body["project_id"]
    task_type = body.get("task_type", "design")
    requirements_raw = body["requirements"]
    feedback = body.get("feedback")
    previous_options_raw = body.get("previous_options")

    store = get_store()

    # --- Mark task as PROCESSING ---
    logger.info("Processing design task %s (type=%s)", task_id, task_type)
    store.update_task(tenant_id, task_id, {
        "status": DesignTaskStatus.PROCESSING.value,
        "started_at": datetime.now(UTC).isoformat(),
    })

    # --- Build requirements model ---
    requirements = InterviewOutput.model_validate(requirements_raw)

    # --- Reconstruct previous options if present (redesign flow) ---
    previous_options: list[DesignOption] | None = None
    if previous_options_raw:
        previous_options = [
            DesignOption.model_validate(opt) for opt in previous_options_raw
        ]

    # --- Discover templates ---
    template_summary, all_prefixes = discover_template_summary(requirements)

    # --- Build agent prompt ---
    prompt = build_agent_prompt(requirements, feedback, previous_options)

    # --- Invoke design agent ---
    start = time.perf_counter()
    result = design_agent(
        prompt,
        available_templates=template_summary,
        invocation_state={"tenant_id": tenant_id, "project_id": project_id},
    )
    duration_ms = (time.perf_counter() - start) * 1000
    logger.info(
        "Design agent completed for task %s in %.0fms", task_id, duration_ms,
    )

    # --- Extract recommendation ---
    recommendation = extract_recommendation(result, all_prefixes)

    # --- Save result ---
    now = datetime.now(UTC).isoformat()
    result_dict = recommendation.model_dump()

    store.update_task(tenant_id, task_id, {
        "status": DesignTaskStatus.COMPLETED.value,
        "completed_at": now,
        "result": result_dict,
    })

    store.save_step(tenant_id, project_id, "design", result_dict, advance=False)

    # Clear the active task pointer now that generation is done
    project = store.get_project(tenant_id, project_id)
    if project:
        project.active_design_task_id = None
        store.update_project(project)

    logger.info("Design task %s completed successfully", task_id)

    # --- Notify subscribers ---
    if notify_fn:
        notify_fn(tenant_id, project_id, task_id, "completed")


def mark_task_failed(
    body: dict,
    notify_fn: Callable[[str, str, str, str], None] | None = None,
) -> None:
    """Mark a task as FAILED in the store. Best-effort — does not raise."""
    try:
        task_id = body.get("task_id")
        tenant_id = body.get("tenant_id")
        if not task_id or not tenant_id:
            logger.error("Cannot mark task failed: missing task_id or tenant_id")
            return

        store = get_store()
        store.update_task(tenant_id, task_id, {
            "status": DesignTaskStatus.FAILED.value,
            "completed_at": datetime.now(UTC).isoformat(),
            "error_message": "Design generation failed — see logs for details",
        })

        project_id = body.get("project_id")
        if project_id:
            project = store.get_project(tenant_id, project_id)
            if project:
                project.active_design_task_id = None
                store.update_project(project)

        if project_id and notify_fn:
            notify_fn(tenant_id, project_id, task_id, "failed")
    except Exception:
        logger.exception("Failed to mark task %s as failed", body.get("task_id"))
