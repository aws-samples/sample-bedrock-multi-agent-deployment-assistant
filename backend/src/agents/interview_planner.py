"""Interview planner — generates a QuestionPlan from seed data + KB search.

Called once per interview session (Turn 1) and again on curveball re-plans.
Uses the primary model (Sonnet) with structured output.
"""

import json
import logging
import time
from pathlib import Path

from strands import Agent

from src.agents.common import bedrock_retry, create_bedrock_model
from src.config.callback import logging_callback_handler
from src.config.circuit_breaker import bedrock_breaker
from src.config.settings import settings
from src.models.interview_plan import (
    PlannedQuestion,
    QuestionPlan,
    QuestionPlanOutput,
)
from src.models.requirements import (
    UseCases,
    get_missing_fields_schema,
    get_seed_context_block,
)
from src.tools.kb_search import KBResult, kb_search_filtered

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
_plan_prompt_template = (PROMPTS_DIR / "interview_plan.txt").read_text()
_replan_prompt_template = (PROMPTS_DIR / "interview_replan.txt").read_text()


def _build_enum_reference() -> str:
    """Build the enum values reference block from the catalog.

    Reads blocking base fields that have options (enum type) from the catalog
    and formats them for the LLM prompt.
    """
    from src.services.catalog_loader import get_catalog
    catalog = get_catalog()

    lines: list[str] = []
    for field in catalog.get_blocking_base_fields():
        if field.options:
            lines.append(
                f"- **{field.name}** (`expected_type: \"enum\"`): "
                f"`{json.dumps(field.options)}`"
            )

    # Also include use case values
    uc_values = catalog.get_use_case_values()
    if uc_values:
        lines.append(
            f"- **use_cases** (`expected_type: \"list_str\"`): "
            f"`{json.dumps(uc_values)}`"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# KB search helpers
# ---------------------------------------------------------------------------


def _search_kb_for_planning(
    use_cases: list, seed_data: dict
) -> list[KBResult]:
    """Level-1 hierarchical KB search: architecture + components per use case."""
    from src.services.catalog_loader import get_catalog
    catalog = get_catalog()

    results: list[KBResult] = []
    desc = seed_data.get("solution_description", "")
    for uc in use_cases:
        uc_val = uc.value if hasattr(uc, "value") else str(uc)
        if uc_val == "notknown":
            continue
        query = catalog.format_search_query(uc_val) + f" {desc}".strip()
        results.extend(
            kb_search_filtered(
                query,
                use_case=uc_val,
                document_type=["architecture", "components"],
                max_results=5,
            )
        )
    return results


def _search_kb_for_replan(
    use_cases: list,
    deviation_reason: str,
    deployment_type: str | None = None,
) -> list[KBResult]:
    """Level-3 KB search: narrowed by deployment type if known."""
    from src.services.catalog_loader import get_catalog
    catalog = get_catalog()

    results: list[KBResult] = []
    for uc in use_cases:
        uc_val = uc.value if hasattr(uc, "value") else str(uc)
        if uc_val == "notknown":
            continue
        query = catalog.format_search_query(uc_val) + f" {deviation_reason}"
        results.extend(
            kb_search_filtered(
                query,
                use_case=uc_val,
                deployment_type=deployment_type,
                max_results=5,
            )
        )
    return results


def _format_kb_results(results: list[KBResult]) -> str:
    """Format KB results for prompt injection."""
    if not results:
        return "No knowledge base results available."
    parts = []
    for r in results:
        parts.append(f"[Source: {r.source_uri} | Score: {r.score:.2f}]\n{r.text}")
    return "\n---\n".join(parts)


# ---------------------------------------------------------------------------
# Plan generation
# ---------------------------------------------------------------------------


def _build_planning_prompt(
    use_cases: list,
    seed_data: dict,
    kb_results: list[KBResult],
    populated_fields: dict | None,
) -> str:
    """Build the system prompt for Sonnet plan generation."""
    from src.services.catalog_loader import get_catalog
    catalog = get_catalog()

    schema = get_missing_fields_schema(use_cases, populated_fields or {})
    schema_text = json.dumps(schema.get("properties", {}), indent=2)

    format_vars = {
        **catalog.get_prompt_context(),
        "seed_context_block": get_seed_context_block(use_cases, seed_data),
        "kb_results": _format_kb_results(kb_results),
        "missing_fields_schema": schema_text,
        "enum_reference": _build_enum_reference(),
    }
    return _plan_prompt_template.format(**format_vars)


def _enrich_plan(
    output: QuestionPlanOutput,
    use_cases: list,
    seed_data: dict,
) -> QuestionPlan:
    """Convert LLM output to a fully enriched QuestionPlan.

    Adds is_blocking/is_optional from the catalog (the LLM doesn't
    determine these — the schema does).
    """
    from src.services.catalog_loader import get_catalog
    catalog = get_catalog()

    # Build a set of optional field paths from the catalog
    optional_paths: set[str] = {"user_info", "user_info.name", "user_info.experience_on_cloud", "compliance"}
    for uc in use_cases:
        uc_val = uc.value if hasattr(uc, "value") else str(uc)
        spec = catalog.get_use_case_spec(uc_val)
        if not spec:
            continue
        for f in spec.optional_fields:
            optional_paths.add(f"{uc_val}.{f}")

    entries: list[PlannedQuestion] = []
    for q in output.questions:
        is_optional = q.field_path in optional_paths
        entries.append(PlannedQuestion(
            field_path=q.field_path,
            question_template=q.question_template,
            kb_context=q.kb_context,
            expected_type=q.expected_type,
            valid_values=q.valid_values,
            is_blocking=not is_optional,
            is_optional=is_optional,
            skip_conditions=q.skip_conditions,
        ))

    # Seed data is pre-populated
    populated = dict(seed_data)
    # Auto-filled fields are also populated
    for path, value in output.auto_filled_fields.items():
        populated[path] = value

    return QuestionPlan(
        entries=entries,
        auto_filled=output.auto_filled_fields,
        auto_fill_rationale=output.auto_fill_rationale,
        kb_summary=output.kb_summary,
        populated_fields=populated,
    )


@bedrock_retry("interview-planner")
def _invoke_planner(agent: Agent, prompt: str) -> object:
    return bedrock_breaker.call(agent, prompt)


def generate_plan(
    seed_data: dict,
    use_cases: list,
    populated_fields: dict | None = None,
    tenant_id: str = "default",
) -> tuple[QuestionPlan, str]:
    """Generate a QuestionPlan from seed data + KB search.

    Returns (plan, initial_message).
    """
    from src.config.metrics import metrics

    bedrock_breaker.pre_check()

    kb_results = _search_kb_for_planning(use_cases, seed_data)
    system_prompt = _build_planning_prompt(use_cases, seed_data, kb_results, populated_fields)

    agent = Agent(
        name="interview-planner",
        model=create_bedrock_model(settings.interview_max_tokens),
        system_prompt=system_prompt,
        tools=[],
        structured_output_model=QuestionPlanOutput,
        callback_handler=logging_callback_handler,
    )

    start = time.perf_counter()
    result = _invoke_planner(agent, "Generate the interview plan based on the seed data and KB results.")
    duration_ms = (time.perf_counter() - start) * 1000
    metrics.record_latency("interview-planner", duration_ms, tenant_id)

    output = getattr(result, "structured_output", None)
    if not isinstance(output, QuestionPlanOutput):
        logger.warning("Planner did not return structured output, using fallback plan")
        return _fallback_plan(use_cases, seed_data, populated_fields), ""

    plan = _enrich_plan(output, use_cases, seed_data)

    # Run initial skip evaluation (some conditions may already be met from seed data)
    plan.evaluate_skip_conditions()

    logger.info(
        "Plan generated: %d questions (%d auto-filled, %d pending)",
        len(plan.entries),
        len(plan.auto_filled),
        plan.pending_count(),
    )
    return plan, output.initial_message


def replan(
    current_plan: QuestionPlan,
    deviation_reason: str,
    use_cases: list,
    tenant_id: str = "default",
) -> tuple[QuestionPlan, str]:
    """Re-plan after a curveball. Preserves answered entries, replaces pending ones.

    Returns (updated_plan, response_message).
    """
    from src.config.metrics import metrics

    bedrock_breaker.pre_check()

    kb_results = _search_kb_for_replan(use_cases, deviation_reason)
    remaining_schema = get_missing_fields_schema(use_cases, current_plan.populated_fields)
    schema_text = json.dumps(remaining_schema.get("properties", {}), indent=2)

    from src.services.catalog_loader import get_catalog
    catalog = get_catalog()
    format_vars = {
        **catalog.get_prompt_context(),
        "deviation_reason": deviation_reason,
        "populated_fields": json.dumps(current_plan.populated_fields, indent=2),
        "kb_results": _format_kb_results(kb_results),
        "remaining_fields_schema": schema_text,
        "enum_reference": _build_enum_reference(),
    }
    system_prompt = _replan_prompt_template.format(**format_vars)

    agent = Agent(
        name="interview-replanner",
        model=create_bedrock_model(settings.interview_max_tokens),
        system_prompt=system_prompt,
        tools=[],
        structured_output_model=QuestionPlanOutput,
        callback_handler=logging_callback_handler,
    )

    start = time.perf_counter()
    result = _invoke_planner(agent, "Re-plan the interview based on the deviation.")
    duration_ms = (time.perf_counter() - start) * 1000
    metrics.record_latency("interview-replanner", duration_ms, tenant_id)

    output = getattr(result, "structured_output", None)
    if not isinstance(output, QuestionPlanOutput):
        logger.warning("Replanner failed structured output — keeping current plan")
        return current_plan, "I've noted your update. Let me continue with the next question."

    # Merge: keep answered entries, replace pending with new plan
    answered = [e for e in current_plan.entries if e.status in ("answered", "auto_filled")]
    new_plan = _enrich_plan(output, use_cases, current_plan.populated_fields)
    new_plan.entries = answered + new_plan.entries
    new_plan.populated_fields = current_plan.populated_fields.copy()
    new_plan.populated_fields.update(output.auto_filled_fields)
    new_plan.replanned_count = current_plan.replanned_count + 1

    new_plan.evaluate_skip_conditions()

    logger.info("Re-planned: %d remaining questions (replan #%d)", new_plan.pending_count(), new_plan.replanned_count)
    return new_plan, output.initial_message


# ---------------------------------------------------------------------------
# Fallback plan (when LLM fails to produce structured output)
# ---------------------------------------------------------------------------


def _fallback_plan(
    use_cases: list,
    seed_data: dict,
    populated_fields: dict | None,
) -> QuestionPlan:
    """Build a minimal plan from the Pydantic schema — no KB context, no skip conditions."""
    schema = get_missing_fields_schema(use_cases, populated_fields or {})
    props = schema.get("properties", {})

    entries: list[PlannedQuestion] = []
    for field_path, info in props.items():
        desc = info.get("description", field_path)
        is_optional = info.get("x-optional", False)
        entries.append(PlannedQuestion(
            field_path=field_path,
            question_template=desc,
            expected_type="str",
            is_blocking=not is_optional,
            is_optional=is_optional,
        ))

    return QuestionPlan(
        entries=entries,
        populated_fields=dict(seed_data) if seed_data else {},
    )
