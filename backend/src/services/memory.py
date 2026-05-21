"""AgentCore Memory integration — session manager factory for Strands agents.

Creates AgentCoreMemorySessionManager instances that plug into Agent(session_manager=...).
When memory is disabled (no ID configured), returns None and agents work statelessly.
"""

import logging

from src.config.settings import settings

logger = logging.getLogger(__name__)


def memory_enabled() -> bool:
    return bool(settings.agentcore_memory_id)


def create_session_manager(
    tenant_id: str,
    project_id: str,
    *,
    fact_top_k: int = 5,
    preference_top_k: int = 3,
):
    """Create an AgentCoreMemorySessionManager for a Strands Agent.

    Returns None if memory is not configured (graceful disable).
    """
    if not memory_enabled():
        return None

    from bedrock_agentcore.memory.integrations.strands.config import (
        AgentCoreMemoryConfig,
        RetrievalConfig,
    )
    from bedrock_agentcore.memory.integrations.strands.session_manager import (
        AgentCoreMemorySessionManager,
    )

    config = AgentCoreMemoryConfig(
        memory_id=settings.agentcore_memory_id,
        session_id=project_id,
        actor_id=tenant_id,
        retrieval_config={
            f"/facts/{tenant_id}/": RetrievalConfig(top_k=fact_top_k, relevance_score=0.5),
            f"/preferences/{tenant_id}/": RetrievalConfig(top_k=preference_top_k, relevance_score=0.6),
        },
        batch_size=5,
    )

    try:
        return AgentCoreMemorySessionManager(config, region_name=settings.aws_region)
    except Exception as e:
        logger.warning("AgentCore Memory unavailable (not yet active?), running stateless: %s", e)
        return None
