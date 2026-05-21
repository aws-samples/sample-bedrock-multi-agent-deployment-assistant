"""Shared test fixtures.

Sets AWS_DEFAULT_REGION before any test module imports Lambda handlers that
call boto3.resource() at module level (ws_notification_bridge, ws_heartbeat).
"""

import os
from datetime import UTC, datetime

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("DYNAMODB_TABLE", "test-table")
os.environ.setdefault("AI_DEPLOY_DEBUG", "true")
# Disable Cognito auth in tests (tests don't send JWT tokens)
os.environ["AI_DEPLOY_COGNITO_USER_POOL_ID"] = ""
os.environ["AI_DEPLOY_COGNITO_CLIENT_ID"] = ""


# ---------------------------------------------------------------------------
# Collection filter: skip heavy test files unless -m integration is passed.
# These transitively import checkov (~170 MB) + cfn-lint (~100 MB) at
# collection time, causing OOM on machines with constrained memory.
# Run with: uv run pytest -m integration tests/
# ---------------------------------------------------------------------------

collect_ignore = ["test_local_worker.py"]


from src.models.design import DesignTask  # noqa: E402
from src.models.docs import DocsTask  # noqa: E402
from src.models.iac import IaCTask  # noqa: E402
from src.models.project import Project, ProjectStatus  # noqa: E402
from src.storage.protocol import STEP_STATUS_MAP, VALID_STEPS  # noqa: E402


class InMemoryProjectStore:
    """Minimal in-memory store for API tests. Not thread-safe."""

    def __init__(self):
        self._projects: dict[str, Project] = {}
        self._steps: dict[str, dict] = {}
        self._tasks: dict[str, DesignTask] = {}
        self._iac_tasks: dict[str, IaCTask] = {}
        self._docs_tasks: dict[str, DocsTask] = {}

    def _key(self, tenant_id: str, project_id: str) -> str:
        return f"{tenant_id}#{project_id}"

    def create_project(self, tenant_id: str, project_id: str, name: str) -> Project:
        p = Project(
            project_id=project_id,
            tenant_id=tenant_id,
            name=name,
            status=ProjectStatus.REQUIREMENTS,
            created_at=datetime.now(UTC).isoformat(),
            updated_at=datetime.now(UTC).isoformat(),
        )
        self._projects[self._key(tenant_id, project_id)] = p
        return p

    def get_project(self, tenant_id: str, project_id: str) -> Project | None:
        return self._projects.get(self._key(tenant_id, project_id))

    def list_projects(
        self, tenant_id: str, limit: int = 50, cursor: str | None = None,
    ) -> tuple[list[Project], str | None]:
        projects = sorted(
            [p for p in self._projects.values() if p.tenant_id == tenant_id],
            key=lambda p: p.updated_at,
            reverse=True,
        )
        start = 0
        if cursor:
            for i, p in enumerate(projects):
                if p.project_id == cursor:
                    start = i + 1
                    break
        page = projects[start : start + limit]
        next_cursor = page[-1].project_id if len(projects) > start + limit else None
        return page, next_cursor

    def delete_project(self, tenant_id: str, project_id: str) -> None:
        self._projects.pop(self._key(tenant_id, project_id), None)

    def update_project(self, project: Project) -> None:
        self._projects[self._key(project.tenant_id, project.project_id)] = project

    def claim_active_task(
        self, tenant_id: str, project_id: str, slot: str, task_id: str,
    ) -> None:
        from src.storage.protocol import ActiveTaskConflictError
        project = self.get_project(tenant_id, project_id)
        if project and getattr(project, slot, None) not in (None, ""):
            raise ActiveTaskConflictError(f"Slot '{slot}' already occupied")
        if project:
            setattr(project, slot, task_id)
            self.update_project(project)

    def save_step(self, tenant_id: str, project_id: str, step: str, data: dict, *, advance: bool = True) -> None:
        if step not in VALID_STEPS:
            raise ValueError(f"Invalid step: {step}")
        self._steps[f"{tenant_id}#{project_id}#{step}"] = data
        if advance:
            project = self.get_project(tenant_id, project_id)
            if project:
                project.status = STEP_STATUS_MAP[step]

    def load_step(self, tenant_id: str, project_id: str, step: str) -> dict | None:
        return self._steps.get(f"{tenant_id}#{project_id}#{step}")

    def create_task(self, tenant_id: str, task: DesignTask) -> None:
        self._tasks[task.task_id] = task

    def get_task(self, tenant_id: str, task_id: str) -> DesignTask | None:
        return self._tasks.get(task_id)

    def update_task(self, tenant_id: str, task_id: str, updates: dict) -> None:
        task = self._tasks.get(task_id)
        if task:
            for k, v in updates.items():
                setattr(task, k, v)

    def create_iac_task(self, tenant_id: str, task: IaCTask) -> None:
        self._iac_tasks[task.task_id] = task

    def get_iac_task(self, tenant_id: str, task_id: str) -> IaCTask | None:
        return self._iac_tasks.get(task_id)

    def update_iac_task(self, tenant_id: str, task_id: str, updates: dict) -> None:
        task = self._iac_tasks.get(task_id)
        if task:
            for k, v in updates.items():
                setattr(task, k, v)

    def create_docs_task(self, tenant_id: str, task: DocsTask) -> None:
        self._docs_tasks[task.task_id] = task

    def get_docs_task(self, tenant_id: str, task_id: str) -> DocsTask | None:
        return self._docs_tasks.get(task_id)

    def update_docs_task(self, tenant_id: str, task_id: str, updates: dict) -> None:
        task = self._docs_tasks.get(task_id)
        if task:
            for k, v in updates.items():
                setattr(task, k, v)


def create_in_memory_store() -> InMemoryProjectStore:
    """Factory for test code that needs to instantiate a store."""
    return InMemoryProjectStore()
