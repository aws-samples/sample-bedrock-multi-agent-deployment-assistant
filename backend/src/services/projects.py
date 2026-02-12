"""Project management services."""

import uuid

from src.storage import get_store


def create_project_service(
    tenant_id: str,
    name: str,
) -> dict:
    """Create a new project and return its dict representation."""
    store = get_store()
    project_id = uuid.uuid4().hex[:12]
    project = store.create_project(tenant_id, project_id, name)
    return project.model_dump()


def list_projects_service(tenant_id: str) -> list[dict]:
    """List all projects for a tenant."""
    store = get_store()
    projects = store.list_projects(tenant_id)
    return [p.model_dump() for p in projects]


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
    return {"status": "deleted"}


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
