"""Shared agent infrastructure — retry logic, model creation, text utilities.

Centralises boilerplate that was duplicated across interview, design, iac, and
documentation agent modules.
"""

from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from strands.models.bedrock import BedrockModel
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.config.agent_hooks import observability_hook
from src.config.guardrails import get_guardrail_kwargs
from src.config.settings import settings

# Transient Bedrock exception types shared by all agent retry decorators
BEDROCK_RETRYABLE = (
    ClientError,
    TimeoutError,
    ConnectionError,
    ReadTimeoutError,
    ConnectTimeoutError,
    EndpointConnectionError,
)


def bedrock_retry(agent_name: str):
    """Tenacity retry decorator for Bedrock agent calls.

    Retries on transient AWS/network errors with exponential backoff.
    Records retry metrics under the given *agent_name*.
    Emits BedrockThrottleCount metric on throttling (HTTP 429).
    """

    def _retry_callback(retry_state) -> None:
        from src.config.metrics import metrics

        metrics.record_retry(agent_name, retry_state.attempt_number)

        exc = retry_state.outcome.exception() if retry_state.outcome else None
        if (
            exc
            and isinstance(exc, ClientError)
            and exc.response.get("Error", {}).get("Code") == "ThrottlingException"
        ):
            metrics.record_bedrock_throttle(agent_name)

    return retry(
        retry=retry_if_exception_type(BEDROCK_RETRYABLE),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        after=_retry_callback,
        reraise=True,
    )


def create_bedrock_model(
    max_tokens: int,
    *,
    include_guardrails: bool = True,
    lightweight: bool = False,
) -> BedrockModel:
    """Create a BedrockModel with standard project settings.

    Args:
        max_tokens: Maximum tokens for the model response.
        include_guardrails: Whether to include guardrail kwargs (default True).
        lightweight: If True, use the lightweight (Haiku) model instead of the
            primary (Sonnet) model. Used by the interview executor and
            refinement planner for fast single-shot turns.
    """
    model_id = settings.lightweight_model_id if lightweight else settings.primary_model_id
    kwargs = get_guardrail_kwargs() if include_guardrails else {}
    return BedrockModel(
        model_id=model_id,
        region_name=settings.aws_region,
        max_tokens=max_tokens,
        **kwargs,
    )


def agent_hooks():
    """Return standard hooks for all agents (observability metrics)."""
    return [observability_hook]


def strip_fences(text: str) -> str:
    """Remove markdown code fences the model may add despite instructions."""
    lines = text.strip().splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)
