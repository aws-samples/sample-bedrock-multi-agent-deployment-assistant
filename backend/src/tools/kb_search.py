"""Knowledge base search — flat tool for agents + filtered search for interview planner.

The @tool-decorated `kb_search` is used by design and other Strands agents.
The plain `kb_search_filtered` is called directly by the interview planner for
hierarchical metadata-filtered searches (use_case, deployment_type, document_type).
"""

import logging
import re
from typing import Any

import boto3
from pydantic import BaseModel
from strands import tool

from src.config.settings import settings

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
    """Search FCCS knowledge base for reference architectures and best practices.

    Args:
        query: The search query for finding relevant knowledge base content.
        max_results: Maximum number of results to return.

    Returns:
        Concatenated text results from the knowledge base, separated by '---'.
    """
    logger.info("kb_search query=%r max_results=%d", query, max_results)

    if not settings.knowledge_base_id:
        logger.info("kb_search: knowledge base not configured, skipping")
        return (
            "Knowledge base not configured. "
            "Using built-in FCCS reference architecture knowledge only."
        )

    client = boto3.client("bedrock-agent-runtime", region_name=settings.aws_region)
    response = client.retrieve(
        knowledgeBaseId=settings.knowledge_base_id,
        retrievalQuery={"text": query},
        retrievalConfiguration={
            "vectorSearchConfiguration": {"numberOfResults": max_results}
        },
    )

    results = []
    for r in response.get("retrievalResults", []):
        text = r.get("content", {}).get("text", "")
        if not text:
            continue
        score = r.get("score", 0)
        source = r.get("location", {}).get("s3Location", {}).get("uri", "unknown source")
        preview = text[:80].replace("\n", " ")
        logger.info("kb_search result: score=%.2f source=%s preview=%r", score, source, preview)
        results.append(f"[Source: {source} | Relevance: {score:.2f}]\n{text}")

    logger.info("kb_search: %d results for query=%r", len(results), query)
    return "\n---\n".join(results) if results else "No results found in knowledge base."


# ---------------------------------------------------------------------------
# Filtered search — used by interview planner for hierarchical queries
# ---------------------------------------------------------------------------

# Pattern: s3://bucket/use_case/deployment_type/filename.ext
_S3_PATH_RE = re.compile(r"s3://[^/]+/([^/]+)/([^/]+)/([^/]+)\.[^.]+$")


def _extract_metadata_from_uri(uri: str) -> dict[str, str | None]:
    """Best-effort metadata extraction from S3 URI path structure."""
    m = _S3_PATH_RE.search(uri)
    if not m:
        return {}
    filename_stem = m.group(3)
    return {
        "use_case": m.group(1),
        "deployment_type": m.group(2),
        "document_type": filename_stem,
    }


def _build_kb_filter(
    use_case: str | None,
    deployment_type: str | None,
    document_type: str | list[str] | None,
) -> dict[str, Any] | None:
    """Build a Bedrock KB metadata filter expression.

    Returns None if no filter criteria are provided.
    """
    conditions: list[dict] = []

    if use_case:
        conditions.append({"equals": {"key": "use_case", "value": use_case}})
    if deployment_type:
        conditions.append({"equals": {"key": "deployment_type", "value": deployment_type}})
    if document_type:
        if isinstance(document_type, list):
            conditions.append({"in": {"key": "document_type", "value": document_type}})
        else:
            conditions.append({"equals": {"key": "document_type", "value": document_type}})

    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"andAll": conditions}


def kb_search_filtered(
    query: str,
    *,
    use_case: str | None = None,
    deployment_type: str | None = None,
    document_type: str | list[str] | None = None,
    max_results: int = 5,
) -> list[KBResult]:
    """Hierarchical KB search with Bedrock metadata filtering.

    Builds a filter from the provided metadata attributes and falls back
    to unfiltered vector search if none are provided.  Returns an empty
    list when the knowledge base is not configured.
    """
    if not settings.knowledge_base_id:
        return []

    kb_filter = _build_kb_filter(use_case, deployment_type, document_type)
    search_config: dict[str, Any] = {"numberOfResults": max_results}
    if kb_filter:
        search_config["filter"] = kb_filter

    logger.info(
        "kb_search_filtered query=%r use_case=%s deployment_type=%s document_type=%s",
        query, use_case, deployment_type, document_type,
    )

    client = boto3.client("bedrock-agent-runtime", region_name=settings.aws_region)
    response = client.retrieve(
        knowledgeBaseId=settings.knowledge_base_id,
        retrievalQuery={"text": query},
        retrievalConfiguration={"vectorSearchConfiguration": search_config},
    )

    results: list[KBResult] = []
    for r in response.get("retrievalResults", []):
        text = r.get("content", {}).get("text", "")
        if not text:
            continue
        score = r.get("score", 0)
        source = r.get("location", {}).get("s3Location", {}).get("uri", "")
        meta = _extract_metadata_from_uri(source)

        results.append(KBResult(
            text=text,
            source_uri=source,
            score=score,
            use_case=meta.get("use_case"),
            deployment_type=meta.get("deployment_type"),
            document_type=meta.get("document_type"),
        ))

    logger.info("kb_search_filtered: %d results", len(results))
    return results
