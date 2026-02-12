import functools
import json

import boto3
from strands import tool

from src.config.settings import settings
from src.utils.validation import validate_artifact_path, validate_safe_id


@functools.cache
def _get_s3_client():
    """Cached boto3 S3 client — avoids creating a new client on every tool invocation."""
    return boto3.client("s3", region_name=settings.aws_region)


@tool(context=True)
def save_artifact(
    content: str,
    artifact_path: str,
    content_type: str = "text/plain",
    tool_context=None,
) -> str:
    """Save a generated artifact to S3 with per-tenant prefix isolation.

    Args:
        content: The artifact content to save.
        artifact_path: Path within the project (e.g., 'terraform/main.tf').
        content_type: MIME type of the content.
        tool_context: Framework-provided context with invocation state.

    Returns:
        S3 URI of the saved artifact.
    """
    state = tool_context.invocation_state if tool_context else {}
    tenant_id = validate_safe_id(state.get("tenant_id", "default"), "tenant_id")
    project_id = validate_safe_id(state.get("project_id", "default"), "project_id")
    artifact_path = validate_artifact_path(artifact_path)

    s3_key = f"{tenant_id}/{project_id}/{artifact_path}"
    s3_uri = f"s3://{settings.s3_artifacts_bucket}/{s3_key}"

    if not settings.s3_artifacts_bucket:
        return f"S3 not configured. Would save to: {s3_uri}"

    client = _get_s3_client()
    client.put_object(
        Bucket=settings.s3_artifacts_bucket,
        Key=s3_key,
        Body=content.encode("utf-8"),
        ContentType=content_type,
        Metadata={"tenant_id": tenant_id, "project_id": project_id},
        ServerSideEncryption="aws:kms",
    )

    return s3_uri


@tool(context=True)
def save_artifacts_batch(artifacts: str, tool_context=None) -> str:
    """Save multiple artifacts to S3 in one call.

    Args:
        artifacts: JSON string with list of {path, content, content_type} objects.
        tool_context: Framework-provided context with invocation state.

    Returns:
        Summary of saved artifact paths.
    """
    state = tool_context.invocation_state if tool_context else {}
    tenant_id = validate_safe_id(state.get("tenant_id", "default"), "tenant_id")
    project_id = validate_safe_id(state.get("project_id", "default"), "project_id")

    if not settings.s3_artifacts_bucket:
        return "S3 not configured. Artifact batch save skipped."

    items = json.loads(artifacts)
    client = _get_s3_client()
    saved = []

    for item in items:
        item_path = validate_artifact_path(item["path"])
        s3_key = f"{tenant_id}/{project_id}/{item_path}"
        client.put_object(
            Bucket=settings.s3_artifacts_bucket,
            Key=s3_key,
            Body=item["content"].encode("utf-8"),
            ContentType=item.get("content_type", "text/plain"),
            Metadata={"tenant_id": tenant_id, "project_id": project_id},
            ServerSideEncryption="aws:kms",
        )
        saved.append(s3_key)

    return f"Saved {len(saved)} artifacts: {', '.join(saved)}"
