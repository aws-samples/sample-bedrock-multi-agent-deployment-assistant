"""Project management API routes."""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.config.auth import get_tenant_id
from src.services.projects import (
    create_project_service,
    delete_project_service,
    get_project_service,
    get_project_state_service,
    list_projects_service,
)
from src.utils.validation import validate_safe_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _validated_project_id(project_id: str) -> str:
    """Validate project_id from URL path parameter."""
    try:
        return validate_safe_id(project_id, "project_id")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project ID format")


class CreateProjectRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)


@router.post("")
def create_project(req: CreateProjectRequest, tenant_id: str = Depends(get_tenant_id)):
    """Create a new project."""
    return create_project_service(tenant_id, req.name)


@router.get("")
def list_projects(
    tenant_id: str = Depends(get_tenant_id),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: Optional[str] = Query(default=None),
):
    """List projects for a tenant with pagination."""
    return list_projects_service(tenant_id, limit=limit, cursor=cursor)


@router.get("/{project_id}")
def get_project(
    project_id: str = Depends(_validated_project_id),
    tenant_id: str = Depends(get_tenant_id),
):
    """Get a single project."""
    try:
        return get_project_service(tenant_id, project_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/{project_id}")
def delete_project(
    project_id: str = Depends(_validated_project_id),
    tenant_id: str = Depends(get_tenant_id),
):
    """Delete a project and all its data."""
    return delete_project_service(tenant_id, project_id)


@router.get("/{project_id}/state")
def get_project_state(
    project_id: str = Depends(_validated_project_id),
    tenant_id: str = Depends(get_tenant_id),
):
    """Get full wizard state for hydration — project + all saved step data."""
    try:
        return get_project_state_service(tenant_id, project_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
