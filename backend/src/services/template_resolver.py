"""Template resolution strategy — determines which generation path to use.

Three paths:
  1. PARAMETERIZE — KB template exists, only fill parameter values
  2. COMPOSE — Snippets cover all resource types, assemble into single template
  3. GENERATE — KB-grounded LLM generation (last resort)
"""

import logging
from enum import Enum

from src.models.design import ResolvedIaCParameters
from src.tools.snippet_discovery import SnippetInfo, discover_snippets

logger = logging.getLogger(__name__)


class TemplatePath(str, Enum):
    PARAMETERIZE = "parameterize"
    COMPOSE = "compose"
    GENERATE = "generate"


# Resource types inferred from resolved parameters
_ALWAYS_REQUIRED = ["vpc", "security"]
_FORTIGATE_TYPE = "fortigate"
_NETWORKING_TYPE = "networking"
_OUTPUTS_TYPE = "outputs"


def _infer_resource_types(params: ResolvedIaCParameters) -> list[str]:
    """Infer required resource types from resolved IaC parameters."""
    types = list(_ALWAYS_REQUIRED)

    # FortiGate instances
    if params.fortigate_instances:
        types.append(_FORTIGATE_TYPE)

    # Networking components based on deployment pattern
    pattern = params.deployment_pattern.lower()
    if any(kw in pattern for kw in ("tgw", "transit", "gwlb", "hub", "spoke")):
        types.append(_NETWORKING_TYPE)

    # Outputs
    types.append(_OUTPUTS_TYPE)

    return types


def resolve_template_path(
    params: ResolvedIaCParameters,
) -> tuple[TemplatePath, dict[str, list[SnippetInfo]]]:
    """Determine which template resolution path to use.

    Returns:
        (path, snippets_by_type) — snippets populated only for COMPOSE path.
    """
    # Path 1: KB template match
    if params.code_template_files:
        template_keys = list(params.code_template_files.keys())
        has_cfn = any(
            k.endswith((".yaml", ".yml", ".json")) and "template" in k.lower()
            for k in template_keys
        )
        if has_cfn:
            logger.info("Template resolution: PARAMETERIZE (KB template found: %s)", template_keys)
            return TemplatePath.PARAMETERIZE, {}

    # Path 2: Check snippet coverage
    required_types = _infer_resource_types(params)
    snippets = discover_snippets(required_types)
    coverage = sum(1 for t in required_types if snippets.get(t))

    if coverage == len(required_types):
        logger.info(
            "Template resolution: COMPOSE (snippets cover %d/%d types)",
            coverage, len(required_types),
        )
        return TemplatePath.COMPOSE, snippets

    # Path 3: KB-grounded generation
    logger.info(
        "Template resolution: GENERATE (snippet coverage %d/%d, missing: %s)",
        coverage, len(required_types),
        [t for t in required_types if not snippets.get(t)],
    )
    return TemplatePath.GENERATE, snippets
