"""Bedrock Guardrails configuration for agent models.

When AI_LCM_GUARDRAIL_ID is set, all agents automatically apply guardrails.
The guardrail should be created in the AWS Bedrock console with these settings:

- Content filters: Block hate, insults, sexual, violence (HIGH threshold)
- Denied topics: Competitor products, non-FortiGate discussions
- PII masking: Redact emails, phone numbers, AWS account IDs
- Grounding check: Verify outputs align with KB sources (threshold 0.7)
- Word filters: Block competitor brand names
"""

from typing import Any

from src.config.settings import settings


def get_guardrail_kwargs() -> dict[str, Any]:
    """Return BedrockModel kwargs for guardrail configuration.

    Returns an empty dict if no guardrail is configured, so callers can
    safely unpack: BedrockModel(**base_kwargs, **get_guardrail_kwargs())
    """
    if not settings.guardrail_id:
        return {}

    return {
        "guardrail_id": settings.guardrail_id,
        "guardrail_version": settings.guardrail_version,
        "guardrail_trace": "enabled",
    }
