"""Tool authorization policies for agent pipelines.

Defines which tools each agent is authorized to use, implementing a
least-privilege model. This module provides application-level
enforcement as defense-in-depth.
"""

import logging
from typing import Callable

logger = logging.getLogger(__name__)

# Tool authorization matrix — maps agent names to allowed tool function names.
# Only agents that are created with tools= need entries here.
# Agents with tools=[] or no tools param are listed for completeness
# and to ensure fail-closed behavior if they ever gain tools.
TOOL_POLICIES: dict[str, set[str]] = {
    # Active: design-agent passes tools via get_authorized_tools()
    "design-agent": {"kb_search", "evaluate_design_against_wa"},
    # No-tool agents (tools=[] or tools omitted)
    "interview-planner": set(),
    "interview-executor": set(),
    "interview-replanner": set(),
    "refinement-planner": set(),
    "docs-diagram": set(),
    "docs-diagram-fix": set(),
    "docs-user-guide": set(),
    # IaC sub-agents (no tools — structured output only)
    "iac-compose": set(),
    "iac-layer-plan": set(),
    "iac-layer-generate": set(),
    "iac-layer-fix": set(),
    "iac-fix": set(),
}

# Tools that should NEVER be available to any agent
DENIED_TOOLS: set[str] = {
    "aws_deploy",
    "execute_command",
    "shell",
    "run_code",
}


def validate_tool_assignment(agent_name: str, tools: list[Callable]) -> list[Callable]:
    """Filter tools to only those authorized for the given agent.

    Args:
        agent_name: The name of the agent being configured.
        tools: List of tool functions to validate.

    Returns:
        Filtered list containing only authorized tools.
    """
    allowed = TOOL_POLICIES.get(agent_name)
    if allowed is None:
        logger.error(
            "No tool policy defined for agent '%s' — denying all tools (fail-closed)",
            agent_name,
        )
        return []

    filtered = []
    for tool_fn in tools:
        tool_name = getattr(tool_fn, "tool_name", getattr(tool_fn, "__name__", str(tool_fn)))

        if tool_name in DENIED_TOOLS:
            logger.warning(
                "Blocked denied tool '%s' from agent '%s'",
                tool_name,
                agent_name,
            )
            continue

        if tool_name in allowed:
            filtered.append(tool_fn)
        else:
            logger.warning(
                "Tool '%s' not authorized for agent '%s' — skipping",
                tool_name,
                agent_name,
            )

    return filtered


def get_authorized_tools(agent_name: str, available_tools: list[Callable]) -> list[Callable]:
    """Convenience wrapper: validate and return authorized tools for an agent.

    Usage:
        tools = get_authorized_tools("design-agent", [kb_search, save_artifact])
        agent = Agent(name="design-agent", tools=tools, ...)
    """
    return validate_tool_assignment(agent_name, available_tools)
