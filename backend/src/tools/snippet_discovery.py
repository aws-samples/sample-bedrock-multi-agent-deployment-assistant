"""Composable CFT snippet discovery from S3 knowledge base bucket.

Snippets are organized at:
    s3://{bucket}/snippets/cloudformation/{resource_type}/*.yaml

Each resource type directory may contain multiple snippet variants
(e.g., vpc-2az.yaml, vpc-3az.yaml).
"""

import logging
import threading
import time

import boto3
from botocore.exceptions import ClientError, EndpointConnectionError
from pydantic import BaseModel

from src.config.settings import settings

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 300  # 5 minutes
_SNIPPET_PREFIX = "snippets/cloudformation/"


class SnippetInfo(BaseModel):
    """Metadata for a discovered CFT snippet."""

    resource_type: str       # e.g., "vpc", "compute", "networking"
    filename: str            # e.g., "vpc-2az.yaml"
    s3_key: str              # Full S3 key
    s3_bucket: str


# ---------------------------------------------------------------------------
# Module-level cache (thread-safe)
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[float, dict[str, list[SnippetInfo]]]] = {}
_cache_lock = threading.Lock()


def _get_cached(cache_key: str) -> dict[str, list[SnippetInfo]] | None:
    with _cache_lock:
        entry = _cache.get(cache_key)
        if entry is None:
            return None
        timestamp, data = entry
        if time.monotonic() - timestamp > _CACHE_TTL_SECONDS:
            del _cache[cache_key]
            return None
        return data


def _set_cached(cache_key: str, data: dict[str, list[SnippetInfo]]) -> None:
    with _cache_lock:
        _cache[cache_key] = (time.monotonic(), data)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def discover_snippets(
    resource_types: list[str],
    s3_bucket: str | None = None,
) -> dict[str, list[SnippetInfo]]:
    """Discover composable CFT snippets for specific resource types.

    Scans s3://{bucket}/snippets/cloudformation/{resource_type}/*.yaml

    Returns: { "vpc": [SnippetInfo(...)], "compute": [SnippetInfo(...)] }
    Empty dict when bucket is not configured or S3 is unreachable.
    """
    bucket = s3_bucket or settings.s3_knowledge_base_bucket
    if not bucket:
        logger.info("discover_snippets: S3 knowledge base bucket not configured")
        return {}

    cache_key = f"{bucket}:{','.join(sorted(resource_types))}"
    cached = _get_cached(cache_key)
    if cached is not None:
        logger.debug("discover_snippets: returning cached results")
        return cached

    logger.info("discover_snippets: scanning bucket=%s types=%s", bucket, resource_types)

    try:
        s3_client = boto3.client("s3", region_name=settings.aws_region)
        result: dict[str, list[SnippetInfo]] = {}

        for resource_type in resource_types:
            prefix = f"{_SNIPPET_PREFIX}{resource_type}/"
            snippets: list[SnippetInfo] = []

            paginator = s3_client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    filename = key.split("/")[-1]
                    if filename.endswith((".yaml", ".yml", ".json")):
                        snippets.append(SnippetInfo(
                            resource_type=resource_type,
                            filename=filename,
                            s3_key=key,
                            s3_bucket=bucket,
                        ))

            result[resource_type] = snippets
            if snippets:
                logger.info(
                    "discover_snippets: found %d snippet(s) for %s",
                    len(snippets), resource_type,
                )

        _set_cached(cache_key, result)
        return result

    except (ClientError, EndpointConnectionError) as exc:
        logger.warning("discover_snippets: S3 not accessible (%s)", exc)
        return {}


def fetch_snippet_content(snippet: SnippetInfo) -> str | None:
    """Download snippet content from S3. Returns None on error."""
    try:
        s3_client = boto3.client("s3", region_name=settings.aws_region)
        response = s3_client.get_object(Bucket=snippet.s3_bucket, Key=snippet.s3_key)
        return response["Body"].read().decode("utf-8")
    except (ClientError, EndpointConnectionError) as exc:
        logger.warning("fetch_snippet_content: failed for %s (%s)", snippet.s3_key, exc)
        return None


def fetch_all_snippets(
    snippets_by_type: dict[str, list[SnippetInfo]],
) -> dict[str, list[tuple[SnippetInfo, str]]]:
    """Fetch content for all snippets. Returns type -> [(info, content)] mapping."""
    result: dict[str, list[tuple[SnippetInfo, str]]] = {}
    for resource_type, snippets in snippets_by_type.items():
        fetched: list[tuple[SnippetInfo, str]] = []
        for snippet in snippets:
            content = fetch_snippet_content(snippet)
            if content:
                fetched.append((snippet, content))
        result[resource_type] = fetched
    return result
