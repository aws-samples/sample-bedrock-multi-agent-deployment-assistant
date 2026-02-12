"""Configuration API routes — public metadata endpoints."""

from fastapi import APIRouter

from src.models.requirements import get_use_case_config

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("/use-cases")
def list_use_cases():
    """Return available use-case options for the frontend form."""
    return get_use_case_config()
