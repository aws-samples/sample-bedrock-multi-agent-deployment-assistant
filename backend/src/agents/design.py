"""Design agent — KB-grounded architecture design generation.

Generates 3 design options with topology blueprints, KB citations, and
Well-Architected assessments. Pre-processes KB search results and available
templates into the prompt. Post-validates cross-referential integrity.
"""

import logging
import time
from pathlib import Path

from strands import Agent

from src.agents.common import bedrock_retry, create_bedrock_model
from src.config.callback import logging_callback_handler
from src.config.circuit_breaker import bedrock_breaker
from src.config.settings import settings
from src.config.tool_policies import get_authorized_tools
from src.models.design import DesignRecommendation
from src.tools.kb_search import kb_search
from src.tools.well_architected import evaluate_design_against_wa

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
_system_prompt_template = (PROMPTS_DIR / "design.txt").read_text()


def _build_system_prompt(available_templates: str) -> str:
    """Inject catalog context and available templates into the system prompt."""
    from src.services.catalog_loader import get_catalog
    catalog = get_catalog()
    format_vars = {
        **catalog.get_prompt_context(),
        "available_templates": available_templates,
    }
    return _system_prompt_template.format(**format_vars)


def create_design_agent(available_templates: str = "None") -> Agent:
    """Create a fresh Design Agent instance.

    A new Agent is created per invocation to prevent cross-tenant
    conversation history leakage.
    """
    return Agent(
        name="design-agent",
        model=create_bedrock_model(settings.max_tokens),
        system_prompt=_build_system_prompt(available_templates),
        tools=get_authorized_tools("design-agent", [kb_search, evaluate_design_against_wa]),
        structured_output_model=DesignRecommendation,
        callback_handler=logging_callback_handler,
    )


@bedrock_retry("design")
def _invoke_with_retry(agent: Agent, prompt: str, **kwargs) -> object:
    return bedrock_breaker.call(agent, prompt, **kwargs)


def design_agent(prompt: str, available_templates: str = "None", **kwargs) -> object:
    from src.config.metrics import metrics

    bedrock_breaker.pre_check()
    agent = create_design_agent(available_templates)
    tenant_id = (kwargs.get("invocation_state") or {}).get("tenant_id", "default")

    start = time.perf_counter()
    result = _invoke_with_retry(agent, prompt, **kwargs)
    duration_ms = (time.perf_counter() - start) * 1000

    metrics.record_latency("design", duration_ms, tenant_id)
    return result
