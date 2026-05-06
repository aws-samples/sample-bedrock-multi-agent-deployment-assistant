"""Knowledge base search — flat tool for agents + filtered search for interview planner.

The @tool-decorated `kb_search` is used by design and other Strands agents.
The plain `kb_search_filtered` is called directly by the interview planner for
hierarchical metadata-filtered searches (use_case, deployment_type, document_type).

Both functions delegate to the active KnowledgeBaseProvider (Bedrock in production,
local file search in development, or null when unconfigured).
"""

import logging

from pydantic import BaseModel
from strands import tool

from src.services.kb_provider import KBSearchResult, get_kb_provider

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Structured result model (used by interview planner)
# ---------------------------------------------------------------------------


class KBResult(BaseModel):
    """A single knowledge base search result with extracted metadata."""

    text: str
    source_uri: str
    score: float = 0.0
    use_case: str | None = None
    deployment_type: str | None = None
    document_type: str | None = None


# ---------------------------------------------------------------------------
# Original tool — used by design agent and others (unchanged contract)
# ---------------------------------------------------------------------------


@tool
def kb_search(query: str, max_results: int = 5) -> str:
    """Search knowledge base for reference architectures and best practices.

    Args:
        query: The search query for finding relevant knowledge base content.
        max_results: Maximum number of results to return.

    Returns:
        Concatenated text results from the knowledge base, separated by '---'.
    """
    logger.info("kb_search query=%r max_results=%d", query, max_results)

    provider = get_kb_provider()
    if not provider.is_available:  # nosemgrep: is-function-without-parentheses - is_available is a @property
        logger.info("kb_search: knowledge base not configured, skipping")
        return (
            "Knowledge base not configured. "
            "Using built-in reference architecture knowledge only."
        )

    results = provider.search(query, max_results=max_results)

    formatted = []
    for r in results:
        preview = r.text[:80].replace("\n", " ")
        logger.info("kb_search result: score=%.2f source=%s preview=%r", r.score, r.source_uri, preview)
        formatted.append(f"[Source: {r.source_uri} | Relevance: {r.score:.2f}]\n{r.text}")

    logger.info("kb_search: %d results for query=%r", len(formatted), query)
    return "\n---\n".join(formatted) if formatted else "No results found in knowledge base."


# ---------------------------------------------------------------------------
# Filtered search — used by interview planner for hierarchical queries
# ---------------------------------------------------------------------------


def kb_search_filtered(
    query: str,
    *,
    use_case: str | None = None,
    deployment_type: str | None = None,
    document_type: str | list[str] | None = None,
    max_results: int = 5,
) -> list[KBResult]:
    """Hierarchical KB search with metadata filtering.

    Delegates to the active provider (Bedrock with metadata filters in production,
    local file search with path-based filtering in development).
    Returns an empty list when no provider is available.
    """
    provider = get_kb_provider()
    if not provider.is_available:  # nosemgrep: is-function-without-parentheses - is_available is a @property
        return []

    logger.info(
        "kb_search_filtered query=%r use_case=%s deployment_type=%s document_type=%s",
        query, use_case, deployment_type, document_type,
    )

    raw_results: list[KBSearchResult] = provider.search(
        query,
        max_results=max_results,
        use_case=use_case,
        deployment_type=deployment_type,
        document_type=document_type,
    )

    results: list[KBResult] = [
        KBResult(
            text=r.text,
            source_uri=r.source_uri,
            score=r.score,
            use_case=r.use_case,
            deployment_type=r.deployment_type,
            document_type=r.document_type,
        )
        for r in raw_results
    ]

    logger.info("kb_search_filtered: %d results", len(results))
    return results
