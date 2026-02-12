"""Input sanitization for user-provided text before LLM invocation."""

import os
import re

from fastapi import HTTPException

MAX_FIELD_LENGTH = 10_000
MAX_TOTAL_PAYLOAD = 50_000

# ---------------------------------------------------------------------------
# ID and path validation (shared by all storage backends + tools)
# ---------------------------------------------------------------------------

_SAFE_ID_PATTERN = re.compile(r'^[a-zA-Z0-9_-]+$')
_SAFE_PATH_PATTERN = re.compile(r'^[a-zA-Z0-9._/ -]+$')


def validate_safe_id(value: str, field_name: str = "id") -> str:
    """Validate that a tenant_id or project_id is safe for use in storage keys.

    Allows alphanumeric, hyphens, and underscores only.
    Raises ValueError on invalid input.
    """
    if not value or not _SAFE_ID_PATTERN.match(value):
        raise ValueError(f"Invalid {field_name}: {value!r}")
    return value


def validate_artifact_path(path: str) -> str:
    """Validate an artifact path is safe for S3 key construction.

    Rejects path traversal (..), leading slashes, and non-printable characters.
    Returns the normalized path.
    """
    if not path or not path.strip():
        raise ValueError("Artifact path cannot be empty")

    normalized = os.path.normpath(path).replace("\\", "/").lstrip("/")

    if ".." in normalized.split("/"):
        raise ValueError(f"Path traversal detected in artifact path: {path!r}")

    if not _SAFE_PATH_PATTERN.match(normalized):
        raise ValueError(f"Invalid characters in artifact path: {path!r}")

    return normalized

# Patterns commonly used in prompt injection attempts
_INJECTION_PATTERNS = re.compile(
    r"(ignore\s+(all\s+)?(previous|above|prior)\s+(instructions|prompts|rules))"
    r"|(you\s+are\s+now\b)"
    r"|(act\s+as\s+(if\s+you\s+are|a)\b)"
    r"|(system\s*:\s*)"
    r"|(```\s*(system|assistant)\b)",
    re.IGNORECASE,
)


def sanitize_text(text: str, field_name: str = "input") -> str:
    """Sanitize a single text field: enforce length limit and strip injection patterns."""
    if len(text) > MAX_FIELD_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} exceeds maximum length of {MAX_FIELD_LENGTH} characters",
        )
    return _INJECTION_PATTERNS.sub("", text).strip()


def sanitize_requirements(data: dict) -> dict:
    """Sanitize all string fields in a requirements/design payload, including nested dicts."""
    def sanitize_value(key: str, value):
        if isinstance(value, str):
            return sanitize_text(value, field_name=key)
        if isinstance(value, list):
            return [
                sanitize_text(v, field_name=f"{key}[]") if isinstance(v, str)
                else sanitize_requirements(v) if isinstance(v, dict)
                else v
                for v in value
            ]
        if isinstance(value, dict):
            return sanitize_requirements(value)
        return value

    def calculate_size(value) -> int:
        if isinstance(value, str):
            return len(value)
        if isinstance(value, list):
            return sum(calculate_size(v) for v in value)
        if isinstance(value, dict):
            return sum(calculate_size(v) for v in value.values())
        return 0

    sanitized = {key: sanitize_value(key, value) for key, value in data.items()}
    total_len = calculate_size(sanitized)

    if total_len > MAX_TOTAL_PAYLOAD:
        raise HTTPException(
            status_code=400,
            detail=f"Total payload size exceeds {MAX_TOTAL_PAYLOAD} characters",
        )
    return sanitized
