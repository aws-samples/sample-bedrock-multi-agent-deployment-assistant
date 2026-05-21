"""Documentation agent — generates 2 deliverables after IaC generation.

Deliverables (generated in parallel via asyncio.gather):
  1. Architecture Diagram — Mermaid architecture-beta with AWS icons + validation-fix loop
  2. User Guide — comprehensive deployment/readme guide (single LLM call)

Each section notifies the caller via callback as it completes, enabling
progressive rendering on the frontend.

The diagram goes through a validate→fix loop (up to N attempts) using
Node.js mermaid.parse() for real syntax validation — the same parser the
frontend uses to render, so a passing diagram is guaranteed renderable.
"""

import asyncio
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path

from strands import Agent

from src.agents.common import agent_hooks, bedrock_retry, create_bedrock_model, strip_fences
from src.config.callback import logging_callback_handler
from src.config.circuit_breaker import bedrock_breaker
from src.config.settings import settings
from src.models.docs import DocumentationOutput
from src.tools.mermaid_validator import validate_mermaid

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def _load_prompt(name: str) -> str:
    """Load a prompt template and inject catalog context variables."""
    from src.services.catalog_loader import get_catalog
    from src.utils.formatting import PartialFormatMap
    template = (_PROMPTS_DIR / name).read_text()
    try:
        catalog = get_catalog()
        return template.format_map(PartialFormatMap(catalog.get_prompt_context()))
    except Exception:
        return template


# ---------------------------------------------------------------------------
# Diagram system prompt (shared between generate and fix)
# ---------------------------------------------------------------------------

_DIAGRAM_SYSTEM_PROMPT = """\
You are an expert AWS architecture diagram generator. You produce valid Mermaid \
architecture-beta diagrams with official AWS icons for cloud deployments.

Output ONLY the raw Mermaid code. No markdown fences, no explanation, no preamble.\
"""

# ---------------------------------------------------------------------------
# Diagram generation with validation-fix loop
# ---------------------------------------------------------------------------


@bedrock_retry("docs-diagram")
def _generate_diagram(cft_template: str, state: dict) -> str:
    """Generate an architecture diagram via a single LLM call."""
    user_prompt = _load_prompt("docs_diagram.txt").replace(
        "{cft_template}", cft_template
    )
    model = create_bedrock_model(max_tokens=settings.docs_diagram_max_tokens)
    agent = Agent(
        name="docs-diagram",
        model=model,
        system_prompt=_DIAGRAM_SYSTEM_PROMPT,
        callback_handler=logging_callback_handler,
        hooks=agent_hooks(),
    )
    result = bedrock_breaker.call(agent, user_prompt, invocation_state=state)
    return strip_fences(str(result))


@bedrock_retry("docs-diagram-fix")
def _fix_diagram(
    diagram_code: str,
    validation_errors: str,
    cft_template: str,
    state: dict,
) -> str:
    """Fix a diagram based on Mermaid validation errors."""
    user_prompt = (
        _load_prompt("docs_diagram_fix.txt")
        .replace("{diagram_code}", diagram_code)
        .replace("{validation_errors}", validation_errors)
        .replace("{cft_template}", cft_template)
    )
    model = create_bedrock_model(max_tokens=settings.docs_diagram_fix_max_tokens)
    agent = Agent(
        name="docs-diagram-fix",
        model=model,
        system_prompt=_DIAGRAM_SYSTEM_PROMPT,
        callback_handler=logging_callback_handler,
        hooks=agent_hooks(),
    )
    result = bedrock_breaker.call(agent, user_prompt, invocation_state=state)
    return strip_fences(str(result))


async def _generate_and_validate_diagram(
    cft_template: str,
    state: dict,
) -> tuple[str, int, bool]:
    """Generate diagram then run a validate→fix loop.

    Returns:
        (diagram_code, fix_attempts_used, validation_passed)
    """
    if not cft_template or not cft_template.strip():
        return "", 0, False

    max_attempts = settings.docs_diagram_max_fix_attempts
    start = time.perf_counter()

    # Step 1: initial generation
    diagram = await asyncio.to_thread(_generate_diagram, cft_template, state)

    # Step 2: validate → fix loop
    for attempt in range(1, max_attempts + 1):
        valid, errors = await asyncio.to_thread(validate_mermaid, diagram)

        if valid:
            duration_ms = (time.perf_counter() - start) * 1000
            logger.info(
                "Diagram validated on attempt %d/%d (%.0fms)",
                attempt, max_attempts, duration_ms,
            )
            _record_metrics("documentation_diagram", duration_ms, state)
            return diagram, attempt, True

        logger.warning(
            "Diagram validation failed (attempt %d/%d): %s",
            attempt, max_attempts, errors[:200],
        )

        if attempt < max_attempts:
            diagram = await asyncio.to_thread(
                _fix_diagram, diagram, errors, cft_template, state,
            )

    # Exhausted attempts — return best effort
    duration_ms = (time.perf_counter() - start) * 1000
    logger.warning(
        "Diagram validation failed after %d attempts (%.0fms), returning best effort",
        max_attempts, duration_ms,
    )
    _record_metrics("documentation_diagram", duration_ms, state)
    return diagram, max_attempts, False


# ---------------------------------------------------------------------------
# Text section generators
# ---------------------------------------------------------------------------

_TEXT_SYSTEM_PROMPT = """\
You are a Senior Technical Writer producing documentation for a cloud \
deployment on AWS. Output ONLY the requested content in Markdown. \
No preamble, no wrapping fences, no meta-commentary. \
Use tables, bullet points, and numbered lists for clarity. \
Be thorough and detailed — this is production documentation.\
"""


@bedrock_retry("docs-user-guide")
def _generate_user_guide(context: dict[str, str], state: dict) -> str:
    """Generate the complete user guide in a single LLM call."""
    from src.services.memory import create_session_manager

    user_prompt = _load_prompt("docs_user_guide.txt").replace(
        "{design_json}", context["design_json"]
    ).replace(
        "{requirements_json}", context["requirements_json"]
    ).replace(
        "{cft_template}", context["cft_template"]
    )

    tenant_id = state.get("tenant_id", "")
    project_id = state.get("project_id", "")

    model = create_bedrock_model(max_tokens=settings.docs_user_guide_max_tokens)
    agent = Agent(
        name="docs-user-guide",
        model=model,
        system_prompt=_TEXT_SYSTEM_PROMPT,
        callback_handler=logging_callback_handler,
        hooks=agent_hooks(),
        session_manager=create_session_manager(tenant_id, project_id) if project_id else None,
    )
    result = bedrock_breaker.call(agent, user_prompt, invocation_state=state)
    return strip_fences(str(result))




# ---------------------------------------------------------------------------
# Metrics helper
# ---------------------------------------------------------------------------


def _record_metrics(metric_name: str, duration_ms: float, state: dict) -> None:
    from src.config.metrics import metrics

    tenant_id = state.get("tenant_id", "default")
    metrics.record_latency(metric_name, duration_ms, tenant_id)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def generate_documentation(
    design: dict,
    requirements_json: str,
    cft_template: str,
    *,
    tenant_id: str = "default",
    project_id: str = "default",
    on_section_complete: Callable[[str, str], None] | None = None,
) -> DocumentationOutput:
    """Generate documentation deliverables in parallel.

    Runs diagram and user guide concurrently via ``asyncio.gather()``.
    The diagram task internally performs a sequential validate→fix loop
    while the user guide runs alongside it.

    Args:
        design: Approved design option dict.
        requirements_json: Project requirements as a JSON string.
        cft_template: Generated CloudFormation template YAML.
        on_section_complete: Optional callback(section_name, content) called
            as each section finishes. Enables progressive frontend rendering.

    Returns:
        Fully assembled ``DocumentationOutput``.
    """
    bedrock_breaker.pre_check()
    overall_start = time.perf_counter()

    state = {"tenant_id": tenant_id, "project_id": project_id}
    context = {
        "design_json": json.dumps(design, indent=2),
        "requirements_json": requirements_json,
        "cft_template": cft_template,
    }

    # --- Parallel task wrappers (each notifies on completion OR failure) -----

    def _notify(section: str, content: str) -> None:
        if on_section_complete:
            try:
                on_section_complete(section, content)
            except Exception:
                logger.warning("Failed to send section notification for %s", section)

    async def _diagram_task() -> tuple[str, int, bool]:
        try:
            diagram, attempts, passed = await _generate_and_validate_diagram(
                cft_template, state,
            )
            _notify("architecture_diagram", diagram)
            return diagram, attempts, passed
        except Exception as exc:
            error_text = f"*Diagram generation failed: {exc}*"
            _notify("architecture_diagram", error_text)
            raise

    async def _guide_task() -> str:
        try:
            start = time.perf_counter()
            guide = await asyncio.to_thread(_generate_user_guide, context, state)
            _record_metrics("documentation_guide", (time.perf_counter() - start) * 1000, state)
            _notify("user_guide", guide)
            return guide
        except Exception as exc:
            error_text = f"*User guide generation failed: {exc}*"
            _notify("user_guide", error_text)
            raise

    # --- Run diagram + guide in parallel -------------------------------------

    results = await asyncio.gather(
        _diagram_task(),
        _guide_task(),
        return_exceptions=True,
    )

    # --- Unpack results with error handling ---------------------------------

    diagram = ""
    diagram_attempts = 0
    diagram_passed = False
    user_guide = ""

    # Diagram result
    if isinstance(results[0], BaseException):
        logger.error("Diagram generation failed: %s", results[0], exc_info=results[0])
        diagram = f"*Diagram generation failed: {results[0]}*"
    else:
        diagram, diagram_attempts, diagram_passed = results[0]

    # User guide result
    if isinstance(results[1], BaseException):
        logger.error("User guide generation failed: %s", results[1], exc_info=results[1])
        user_guide = f"*User guide generation failed: {results[1]}*"
    else:
        user_guide = results[1]

    total_ms = (time.perf_counter() - overall_start) * 1000
    logger.info(
        "Documentation generation complete in %.0fms: "
        "guide=%d chars, diagram=%d chars "
        "(validation=%s, fix_attempts=%d)",
        total_ms,
        len(user_guide),
        len(diagram),
        "passed" if diagram_passed else "failed",
        diagram_attempts,
    )

    return DocumentationOutput(
        user_guide=user_guide,
        architecture_diagram=diagram,
        diagram_fix_attempts=diagram_attempts,
        diagram_validation_passed=diagram_passed,
    )


# ---------------------------------------------------------------------------
# Single-section regeneration
# ---------------------------------------------------------------------------


async def regenerate_section(
    section_name: str,
    design: dict,
    requirements_json: str,
    cft_template: str,
    *,
    tenant_id: str = "default",
    project_id: str = "default",
) -> str:
    """Regenerate a single documentation section.

    Reuses the same private generators as ``generate_documentation()`` but
    runs only the requested section.  Used by the per-section retry endpoint.

    Returns:
        The generated content string for the requested section.

    Raises:
        ValueError: If *section_name* is not a valid section.
    """
    from src.models.docs import VALID_DOC_SECTIONS

    if section_name not in VALID_DOC_SECTIONS:
        raise ValueError(f"Invalid section: {section_name}")

    bedrock_breaker.pre_check()
    state = {"tenant_id": tenant_id, "project_id": project_id}
    context = {
        "design_json": json.dumps(design, indent=2),
        "requirements_json": requirements_json,
        "cft_template": cft_template,
    }

    start = time.perf_counter()

    if section_name == "architecture_diagram":
        diagram, attempts, passed = await _generate_and_validate_diagram(
            cft_template, state,
        )
        content = diagram
        logger.info(
            "Diagram regeneration: %d chars (validation=%s, attempts=%d)",
            len(content), "passed" if passed else "failed", attempts,
        )
    else:
        content = await asyncio.to_thread(_generate_user_guide, context, state)

    duration_ms = (time.perf_counter() - start) * 1000
    _record_metrics(f"documentation_{section_name}_regen", duration_ms, state)
    logger.info("Regenerated %s in %.0fms (%d chars)", section_name, duration_ms, len(content))

    return content
