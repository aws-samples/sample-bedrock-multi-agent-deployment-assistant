"""Export endpoints — download generated artifacts as zip archives."""

import io
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from slowapi import Limiter
from slowapi.util import get_remote_address

from src.config.auth import get_tenant_id
from src.services.export import build_iac_zip_bytes
from src.utils.validation import validate_safe_id

router = APIRouter(prefix="/api/export", tags=["export"])

# Standalone limiter for this router (avoids circular import with main.py)
_limiter = Limiter(key_func=get_remote_address)


@router.get("/{project_id}/iac.zip")
@_limiter.limit("5/minute")
def export_iac_zip(request: Request, project_id: str, tenant_id: str = Depends(get_tenant_id)):
    """Bundle all generated IaC files into a downloadable zip."""
    try:
        project_id = validate_safe_id(project_id, "project_id")
        zip_bytes = build_iac_zip_bytes(tenant_id, project_id)
    except ValueError as e:
        status = 400 if "Invalid" in str(e) else 404
        raise HTTPException(status_code=status, detail=str(e))

    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", project_id)
    return StreamingResponse(
        io.BytesIO(zip_bytes),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_id}-iac.zip"'},
    )
