"""Export services — build downloadable artifacts."""

import io
import os
import re
import zipfile

from src.storage import get_store

_SAFE_FILENAME = re.compile(r"^[a-zA-Z0-9._/-]+$")


def _sanitize_zip_path(path: str) -> str:
    """Normalize a file path for safe inclusion in a zip archive.

    Prevents Zip Slip by rejecting path traversal components.
    """
    normalized = os.path.normpath(path).lstrip("/\\")
    if ".." in normalized.split(os.sep):
        raise ValueError(f"Path traversal detected: {path}")
    return normalized


def build_iac_zip_bytes(tenant_id: str, project_id: str) -> bytes:
    """Bundle all generated IaC files into a zip and return raw bytes.

    Raises ValueError if no IaC files exist for the project.
    """
    store = get_store()
    iac = store.load_step(tenant_id, project_id, "iac")
    if not iac or not iac.get("files"):
        raise ValueError("No IaC files found for this project")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for filepath, content in iac["files"].items():
            safe_path = _sanitize_zip_path(filepath)
            zf.writestr(safe_path, content)

    buf.seek(0)
    return buf.read()
