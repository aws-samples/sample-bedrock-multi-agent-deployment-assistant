"""Interview executor — processes one answer per turn via Haiku.

Each invocation is a single-shot call (no conversation history).  The
QuestionPlan provides all context; the executor parses the answer,
generates a natural response, and detects deviations.
"""

import logging
import time
from pathlib import Path

from strands import Agent

from src.agents.common import bedrock_retry, create_bedrock_model
from src.config.callback import logging_callback_handler
from src.config.circuit_breaker import bedrock_breaker
from src.config.settings import settings
from src.models.interview_plan import PlannedQuestion, QuestionPlan, TurnResponse

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
_execute_prompt_template = (PROMPTS_DIR / "interview_execute.txt").read_text()

_MAX_CLARIFICATION_ATTEMPTS = 2


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def _build_valid_values_block(question: PlannedQuestion) -> str:
    if question.valid_values:
        return f"Valid values: {', '.join(question.valid_values)}"
    return ""


def _build_next_question_block(next_q: PlannedQuestion | None) -> str:
    if next_q:
        return f"Field: {next_q.field_path}\nQuestion: {next_q.question_template}"
    return "This is the LAST question. After acknowledging the answer, summarize all gathered requirements and tell the user you have everything needed."


def _build_execution_prompt(
    current_q: PlannedQuestion,
    next_q: PlannedQuestion | None,
    user_message: str,
) -> str:
    from src.services.catalog_loader import get_catalog
    catalog = get_catalog()
    format_vars = {
        **catalog.get_prompt_context(),
        "field_path": current_q.field_path,
        "question_template": current_q.question_template,
        "expected_type": current_q.expected_type,
        "valid_values_block": _build_valid_values_block(current_q),
        "kb_context": current_q.kb_context or "No specific KB context for this field.",
        "user_message": user_message,
        "next_question_block": _build_next_question_block(next_q),
    }
    return _execute_prompt_template.format(**format_vars)


# ---------------------------------------------------------------------------
# Value validation
# ---------------------------------------------------------------------------


def _validate_parsed_value(response: TurnResponse, question: PlannedQuestion) -> TurnResponse:
    """Server-side type coercion and enum validation.

    Adjusts confidence to 0.0 if the value doesn't match expectations.
    """
    val = response.parsed_value
    if val is None:
        return response  # Accepted skip for optional fields

    match question.expected_type:
        case "int":
            try:
                response.parsed_value = int(val)
            except (ValueError, TypeError):
                response.confidence = 0.0
        case "float":
            try:
                response.parsed_value = float(val)
            except (ValueError, TypeError):
                response.confidence = 0.0
        case "enum":
            if question.valid_values:
                normalized = str(val).strip().lower()
                match = next(
                    (v for v in question.valid_values if v.lower() == normalized),
                    None,
                )
                if match:
                    response.parsed_value = match
                else:
                    response.confidence = 0.0
        case "list_str":
            if isinstance(val, str):
                response.parsed_value = [v.strip() for v in val.split(",") if v.strip()]
            elif not isinstance(val, list):
                response.confidence = 0.0

    return response


# ---------------------------------------------------------------------------
# Agent invocation
# ---------------------------------------------------------------------------


def _create_executor_model():
    """Create a Haiku model for fast execution turns."""
    return create_bedrock_model(
        settings.interview_executor_max_tokens, lightweight=True
    )


@bedrock_retry("interview-executor")
def _invoke_executor(agent: Agent, prompt: str) -> object:
    return bedrock_breaker.call(agent, prompt)


def execute_turn(
    plan: QuestionPlan,
    user_message: str,
    tenant_id: str = "default",
) -> tuple[QuestionPlan, TurnResponse]:
    """Execute a single interview turn: parse answer + generate next question.

    Returns the mutated plan and the Haiku response.
    Raises if no pending questions remain (caller should check plan.blocking_complete()).
    """
    from src.config.metrics import metrics

    bedrock_breaker.pre_check()

    current_q = plan.current_question()
    if current_q is None:
        return plan, TurnResponse(
            response_message="All questions have been answered. Your requirements are complete.",
            confidence=1.0,
        )

    next_q = plan.next_question()
    system_prompt = _build_execution_prompt(current_q, next_q, user_message)

    agent = Agent(
        name="interview-executor",
        model=_create_executor_model(),
        system_prompt=system_prompt,
        tools=[],
        structured_output_model=TurnResponse,
        callback_handler=logging_callback_handler,
    )

    start = time.perf_counter()
    result = _invoke_executor(agent, user_message)
    duration_ms = (time.perf_counter() - start) * 1000
    metrics.record_latency("interview-executor", duration_ms, tenant_id)

    response = getattr(result, "structured_output", None)
    if not isinstance(response, TurnResponse):
        logger.warning("Executor did not return structured output — wrapping raw text")
        response = TurnResponse(
            response_message=str(result),
            confidence=0.0,
        )

    # Server-side validation
    response = _validate_parsed_value(response, current_q)

    # Low confidence — don't advance the plan
    if response.confidence < 0.5 and not response.deviation_detected:
        hint = ""
        if current_q.valid_values:
            hint = f" For example: {current_q.valid_values[0]}."
        response.response_message = (
            f"Could you clarify? {current_q.question_template}{hint}"
        )
        logger.info("Low confidence (%.2f) on '%s' — requesting clarification", response.confidence, current_q.field_path)
        return plan, response

    # Deviation — don't advance, let caller handle re-plan
    if response.deviation_detected:
        logger.info("Deviation detected on '%s': %s", current_q.field_path, response.deviation_reason)
        # Still mark the current answer if confidence is decent
        if response.confidence >= 0.5 and response.parsed_value is not None:
            plan.mark_answered(current_q.field_path, response.parsed_value)
            plan.evaluate_skip_conditions()
        return plan, response

    # Normal — advance the plan
    plan.mark_answered(current_q.field_path, response.parsed_value)
    skipped = plan.evaluate_skip_conditions()
    if skipped:
        logger.info("After answering '%s', skipped: %s", current_q.field_path, skipped)

    return plan, response
