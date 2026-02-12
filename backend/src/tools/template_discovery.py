"""Template discovery — find available code templates in the S3 knowledge base bucket.

Templates are organized by use_case and deployment_type following the convention:
    s3://{bucket}/{use_case}/{deployment_type}/code/template.yaml

Results are cached for 5 minutes to avoid excessive S3 API calls during
multi-step agent workflows.
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


class TemplateInfo(BaseModel):
    """Metadata for a discovered code template directory."""

    use_case: str
    deployment_type: str
    s3_prefix: str
    template_files: list[str]


# ---------------------------------------------------------------------------
# Module-level cache (thread-safe)
# ---------------------------------------------------------------------------

_cache: dict[frozenset[str], tuple[float, dict[str, list[TemplateInfo]]]] = {}
_cache_lock = threading.Lock()


def _get_cached(cache_key: frozenset[str]) -> dict[str, list[TemplateInfo]] | None:
    with _cache_lock:
        entry = _cache.get(cache_key)
        if entry is None:
            return None
        timestamp, data = entry
        if time.monotonic() - timestamp > _CACHE_TTL_SECONDS:
            del _cache[cache_key]
            return None
        return data


def _set_cached(cache_key: frozenset[str], data: dict[str, list[TemplateInfo]]) -> None:
    with _cache_lock:
        _cache[cache_key] = (time.monotonic(), data)


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------


def _list_deployment_types(s3_client, bucket: str, use_case: str) -> list[str]:
    """List deployment_type sub-prefixes that contain a code/ directory."""
    deployment_types: list[str] = []
    paginator = s3_client.get_paginator("list_objects_v2")

    for page in paginator.paginate(Bucket=bucket, Prefix=f"{use_case}/", Delimiter="/"):
        for prefix_entry in page.get("CommonPrefixes", []):
            prefix = prefix_entry["Prefix"]
            parts = prefix.rstrip("/").split("/")
            if len(parts) < 2:
                continue
            deployment_type = parts[1]

            code_check = s3_client.list_objects_v2(
                Bucket=bucket,
                Prefix=f"{use_case}/{deployment_type}/code/",
                MaxKeys=1,
            )
            if code_check.get("KeyCount", 0) > 0:
                deployment_types.append(deployment_type)

    return deployment_types


def _list_template_files(s3_client, bucket: str, code_prefix: str) -> list[str]:
    """List all filenames under a given code prefix."""
    filenames: list[str] = []
    paginator = s3_client.get_paginator("list_objects_v2")

    for page in paginator.paginate(Bucket=bucket, Prefix=code_prefix):
        for obj in page.get("Contents", []):
            relative = obj["Key"][len(code_prefix):]
            if relative:
                filenames.append(relative)

    return filenames


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def discover_templates(
    use_cases: list[str],
    s3_bucket: str | None = None,
) -> dict[str, list[TemplateInfo]]:
    """Discover available code templates in the S3 knowledge base bucket.

    Scans ``s3://{bucket}/{use_case}/*/code/`` for each requested use_case.
    Results are cached for 5 minutes.

    Returns empty dict when the bucket is not configured or S3 is unreachable.
    """
    bucket = s3_bucket or settings.s3_knowledge_base_bucket
    if not bucket:
        logger.info("discover_templates: S3 knowledge base bucket not configured, skipping")
        return {}

    cache_key = frozenset(use_cases)
    cached = _get_cached(cache_key)
    if cached is not None:
        logger.debug("discover_templates: returning cached results for %s", use_cases)
        return cached

    logger.info("discover_templates: scanning bucket=%s use_cases=%s", bucket, use_cases)

    try:
        s3_client = boto3.client("s3", region_name=settings.aws_region)
        result: dict[str, list[TemplateInfo]] = {}

        for use_case in use_cases:
            templates: list[TemplateInfo] = []
            deployment_types = _list_deployment_types(s3_client, bucket, use_case)

            for deployment_type in deployment_types:
                code_prefix = f"{use_case}/{deployment_type}/code/"
                template_files = _list_template_files(s3_client, bucket, code_prefix)

                if "template.yaml" not in template_files:
                    continue

                templates.append(
                    TemplateInfo(
                        use_case=use_case,
                        deployment_type=deployment_type,
                        s3_prefix=code_prefix,
                        template_files=template_files,
                    )
                )
                logger.info(
                    "discover_templates: found template %s/%s with %d file(s)",
                    use_case, deployment_type, len(template_files),
                )

            result[use_case] = templates

        _set_cached(cache_key, result)
        return result

    except (ClientError, EndpointConnectionError) as exc:
        logger.warning("discover_templates: S3 not accessible (%s), returning empty", exc)
        return {}
