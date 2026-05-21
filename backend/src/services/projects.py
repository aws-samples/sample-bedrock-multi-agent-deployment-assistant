"""Project management services."""

import logging
import uuid

from src.storage import get_store

logger = logging.getLogger(__name__)


def create_project_service(
    tenant_id: str,
    name: str,
) -> dict:
    """Create a new project and return its dict representation."""
    store = get_store()
    project_id = uuid.uuid4().hex[:12]
    project = store.create_project(tenant_id, project_id, name)
    return project.model_dump()


def list_projects_service(
    tenant_id: str, limit: int = 50, cursor: str | None = None,
) -> dict:
    """List projects for a tenant with pagination."""
    store = get_store()
    projects, next_cursor = store.list_projects(tenant_id, limit=limit, cursor=cursor)
    result: dict = {"projects": [p.model_dump() for p in projects]}
    if next_cursor:
        result["next_cursor"] = next_cursor
    return result


def get_project_service(tenant_id: str, project_id: str) -> dict:
    """Get a single project. Raises ValueError if not found."""
    store = get_store()
    project = store.get_project(tenant_id, project_id)
    if not project:
        raise ValueError(f"Project not found: {project_id}")
    return project.model_dump()


def delete_project_service(tenant_id: str, project_id: str) -> dict:
    """Delete a project and all its data."""
    store = get_store()
    store.delete_project(tenant_id, project_id)
    _cleanup_memory_events(tenant_id, project_id)
    return {"status": "deleted"}


def _cleanup_memory_events(tenant_id: str, project_id: str) -> None:
    """Delete short-term memory events for the project session. Non-fatal."""
    from src.services.memory import memory_enabled

    if not memory_enabled():
        return

    try:
        from src.config.aws import aws_client
        from src.config.settings import settings

        client = aws_client("bedrock-agentcore")
        resp = client.list_events(
            memoryId=settings.agentcore_memory_id,
            sessionId=project_id,
        )
        for event in resp.get("events", []):
            client.delete_event(
                memoryId=settings.agentcore_memory_id,
                eventId=event["eventId"],
            )
    except Exception:
        logger.warning("Failed to clean up memory events for project %s", project_id, exc_info=True)


def get_project_state_service(tenant_id: str, project_id: str) -> dict:
    """Get full wizard state for hydration. Raises ValueError if not found."""
    store = get_store()
    project = store.get_project(tenant_id, project_id)
    if not project:
        raise ValueError(f"Project not found: {project_id}")

    return {
        "project": project.model_dump(),
        "requirements": store.load_step(tenant_id, project_id, "requirements"),
        "design": store.load_step(tenant_id, project_id, "design"),
        "iac": store.load_step(tenant_id, project_id, "iac"),
        "docs": store.load_step(tenant_id, project_id, "docs"),
    }
