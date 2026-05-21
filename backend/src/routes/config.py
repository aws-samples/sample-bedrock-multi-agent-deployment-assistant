"""Configuration API routes — public metadata endpoints."""

from fastapi import APIRouter

from src.config.settings import settings
from src.models.requirements import get_use_case_config

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("/use-cases")
def list_use_cases():
    """Return available use-case options for the frontend form."""
    return get_use_case_config()


@router.get("/client")
def client_config():
    """Return runtime configuration for the frontend client.

    Allows the frontend to dynamically discover the WebSocket endpoint
    and feature flags without requiring build-time environment variables.
    """
    return {
        "websocket_url": settings.websocket_url,
        "auth_enabled": bool(settings.cognito_user_pool_id),
    }
