"""Storage protocol for project persistence."""

from typing import Protocol

from src.models.design import DesignTask
from src.models.docs import DocsTask
from src.models.iac import IaCTask
from src.models.project import Project, ProjectStatus

# ---------------------------------------------------------------------------
# Shared constants — single source of truth for step→status/next mappings
# ---------------------------------------------------------------------------

VALID_STEPS = {"requirements", "design", "iac", "docs"}

STEP_STATUS_MAP: dict[str, ProjectStatus] = {
    "requirements": ProjectStatus.DESIGN,
    "design": ProjectStatus.IAC,
    "iac": ProjectStatus.DOCUMENTATION,
    "docs": ProjectStatus.COMPLETE,
}

STEP_NEXT_MAP: dict[str, str] = {
    "requirements": "design",
    "design": "iac",
    "iac": "documentation",
    "docs": "documentation",
}


class ActiveTaskConflictError(Exception):
    """Raised when an active task already exists for the given slot."""


class ProjectStore(Protocol):
    """Interface for project persistence backends."""

    def create_project(self, tenant_id: str, project_id: str, name: str) -> Project: ...

    def get_project(self, tenant_id: str, project_id: str) -> Project | None: ...

    def list_projects(
        self, tenant_id: str, limit: int = 50, cursor: str | None = None,
    ) -> tuple[list[Project], str | None]: ...

    def delete_project(self, tenant_id: str, project_id: str) -> None: ...

    def update_project(self, project: Project) -> None: ...

    def claim_active_task(
        self, tenant_id: str, project_id: str, slot: str, task_id: str,
    ) -> None:
        """Atomically set an active task slot (e.g., 'active_iac_task_id') only if empty.

        Raises ActiveTaskConflictError if the slot is already occupied.
        """
        ...

    def save_step(self, tenant_id: str, project_id: str, step: str, data: dict, *, advance: bool = True) -> None: ...

    def load_step(self, tenant_id: str, project_id: str, step: str) -> dict | None: ...

    # --- Design task methods (async design generation) ---

    def create_task(self, tenant_id: str, task: DesignTask) -> None: ...

    def get_task(self, tenant_id: str, task_id: str) -> DesignTask | None: ...

    def update_task(self, tenant_id: str, task_id: str, updates: dict) -> None: ...

    # --- IaC task methods (async IaC generation) ---

    def create_iac_task(self, tenant_id: str, task: IaCTask) -> None: ...

    def get_iac_task(self, tenant_id: str, task_id: str) -> IaCTask | None: ...

    def update_iac_task(self, tenant_id: str, task_id: str, updates: dict) -> None: ...

    # --- Docs task methods (async docs generation) ---

    def create_docs_task(self, tenant_id: str, task: DocsTask) -> None: ...

    def get_docs_task(self, tenant_id: str, task_id: str) -> DocsTask | None: ...

    def update_docs_task(self, tenant_id: str, task_id: str, updates: dict) -> None: ...
