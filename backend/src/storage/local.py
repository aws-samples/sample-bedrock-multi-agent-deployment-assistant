"""File-based project store for local development."""

import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path

from src.models.design import DesignTask
from src.models.docs import DocsTask
from src.models.iac import IaCTask
from src.models.project import Project
from src.storage.protocol import STEP_NEXT_MAP, STEP_STATUS_MAP, VALID_STEPS
from src.utils.validation import validate_safe_id

logger = logging.getLogger(__name__)

DATA_DIR = Path(".local-data")


class LocalProjectStore:
    """Stores projects as JSON files under .local-data/{tenant_id}/{project_id}/."""

    def __init__(self):
        # Per-project locks to prevent race conditions in save_step read-modify-write
        self._locks: dict[str, threading.Lock] = {}
        self._global_lock = threading.Lock()

    def _get_lock(self, tenant_id: str, project_id: str) -> threading.Lock:
        """Get or create a per-project lock for thread-safe read-modify-write."""
        key = f"{tenant_id}/{project_id}"
        with self._global_lock:
            if key not in self._locks:
                self._locks[key] = threading.Lock()
            return self._locks[key]

    def _project_dir(self, tenant_id: str, project_id: str) -> Path:
        validate_safe_id(tenant_id, "tenant_id")
        validate_safe_id(project_id, "project_id")
        result = (DATA_DIR / tenant_id / project_id).resolve()
        if not result.is_relative_to(DATA_DIR.resolve()):
            raise ValueError("Path traversal detected")
        return result

    def _read_project(self, path: Path) -> Project | None:
        meta = path / "project.json"
        if not meta.exists():
            return None
        return Project.model_validate_json(meta.read_text())

    def create_project(self, tenant_id: str, project_id: str, name: str) -> Project:
        d = self._project_dir(tenant_id, project_id)
        d.mkdir(parents=True, exist_ok=True)
        project = Project(tenant_id=tenant_id, project_id=project_id, name=name)
        (d / "project.json").write_text(project.model_dump_json(indent=2))
        return project

    def get_project(self, tenant_id: str, project_id: str) -> Project | None:
        return self._read_project(self._project_dir(tenant_id, project_id))

    def list_projects(self, tenant_id: str) -> list[Project]:
        validate_safe_id(tenant_id, "tenant_id")
        tenant_dir = DATA_DIR / tenant_id
        if not tenant_dir.exists():
            return []
        projects = [
            p for child in sorted(tenant_dir.iterdir())
            if child.is_dir() and (p := self._read_project(child))
        ]
        return projects

    def delete_project(self, tenant_id: str, project_id: str) -> None:
        import shutil

        d = self._project_dir(tenant_id, project_id)
        if d.exists():
            shutil.rmtree(d)
        # Clean up per-project lock to prevent memory leak
        lock_key = f"{tenant_id}/{project_id}"
        self._locks.pop(lock_key, None)

    def update_project(self, project: Project) -> None:
        d = self._project_dir(project.tenant_id, project.project_id)
        d.mkdir(parents=True, exist_ok=True)
        project.updated_at = datetime.now(UTC).isoformat()
        (d / "project.json").write_text(project.model_dump_json(indent=2))

    def save_step(self, tenant_id: str, project_id: str, step: str, data: dict, *, advance: bool = True) -> None:
        if step not in VALID_STEPS:
            raise ValueError(f"Invalid step: {step}")

        lock = self._get_lock(tenant_id, project_id)
        with lock:
            d = self._project_dir(tenant_id, project_id)
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{step}.json").write_text(json.dumps(data, indent=2))

            if advance:
                project = self._read_project(d)
                if project:
                    project.status = STEP_STATUS_MAP.get(step, project.status)
                    project.current_step = STEP_NEXT_MAP.get(step, project.current_step)
                    self.update_project(project)

    def load_step(self, tenant_id: str, project_id: str, step: str) -> dict | None:
        if step not in VALID_STEPS:
            return None
        f = self._project_dir(tenant_id, project_id) / f"{step}.json"
        if not f.exists():
            return None
        return json.loads(f.read_text())

    # --- Design task methods ---

    def _tasks_dir(self, tenant_id: str) -> Path:
        validate_safe_id(tenant_id, "tenant_id")
        result = (DATA_DIR / tenant_id / "tasks").resolve()
        if not result.is_relative_to(DATA_DIR.resolve()):
            raise ValueError("Path traversal detected")
        return result

    def create_task(self, tenant_id: str, task: DesignTask) -> None:
        d = self._tasks_dir(tenant_id)
        d.mkdir(parents=True, exist_ok=True)
        validate_safe_id(task.task_id, "task_id")
        (d / f"{task.task_id}.json").write_text(task.model_dump_json(indent=2))

    def get_task(self, tenant_id: str, task_id: str) -> DesignTask | None:
        validate_safe_id(task_id, "task_id")
        f = self._tasks_dir(tenant_id) / f"{task_id}.json"
        if not f.exists():
            return None
        return DesignTask.model_validate_json(f.read_text())

    def update_task(self, tenant_id: str, task_id: str, updates: dict) -> None:
        validate_safe_id(task_id, "task_id")
        f = self._tasks_dir(tenant_id) / f"{task_id}.json"
        if not f.exists():
            raise ValueError(f"Task {task_id} not found")
        task = DesignTask.model_validate_json(f.read_text())
        task_dict = task.model_dump()
        task_dict.update(updates)
        updated = DesignTask.model_validate(task_dict)
        f.write_text(updated.model_dump_json(indent=2))

    # --- IaC task methods ---

    def _iac_tasks_dir(self, tenant_id: str) -> Path:
        validate_safe_id(tenant_id, "tenant_id")
        result = (DATA_DIR / tenant_id / "_iac_tasks").resolve()
        if not result.is_relative_to(DATA_DIR.resolve()):
            raise ValueError("Path traversal detected")
        return result

    def create_iac_task(self, tenant_id: str, task: IaCTask) -> None:
        """Store IaC task as JSON file."""
        d = self._iac_tasks_dir(tenant_id)
        d.mkdir(parents=True, exist_ok=True)
        validate_safe_id(task.task_id, "task_id")
        (d / f"{task.task_id}.json").write_text(task.model_dump_json(indent=2))

    def get_iac_task(self, tenant_id: str, task_id: str) -> IaCTask | None:
        """Read IaC task from local storage."""
        validate_safe_id(task_id, "task_id")
        f = self._iac_tasks_dir(tenant_id) / f"{task_id}.json"
        if not f.exists():
            return None
        return IaCTask.model_validate_json(f.read_text())

    def update_iac_task(self, tenant_id: str, task_id: str, updates: dict) -> None:
        """Update IaC task fields."""
        validate_safe_id(task_id, "task_id")
        f = self._iac_tasks_dir(tenant_id) / f"{task_id}.json"
        if not f.exists():
            raise ValueError(f"IaC task {task_id} not found")
        task = IaCTask.model_validate_json(f.read_text())
        task_dict = task.model_dump()
        task_dict.update(updates)
        updated = IaCTask.model_validate(task_dict)
        f.write_text(updated.model_dump_json(indent=2))

    # --- Docs task methods ---

    def _docs_tasks_dir(self, tenant_id: str) -> Path:
        validate_safe_id(tenant_id, "tenant_id")
        result = (DATA_DIR / tenant_id / "_docs_tasks").resolve()
        if not result.is_relative_to(DATA_DIR.resolve()):
            raise ValueError("Path traversal detected")
        return result

    def create_docs_task(self, tenant_id: str, task: DocsTask) -> None:
        """Store docs task as JSON file."""
        d = self._docs_tasks_dir(tenant_id)
        d.mkdir(parents=True, exist_ok=True)
        validate_safe_id(task.task_id, "task_id")
        (d / f"{task.task_id}.json").write_text(task.model_dump_json(indent=2))

    def get_docs_task(self, tenant_id: str, task_id: str) -> DocsTask | None:
        """Read docs task from local storage."""
        validate_safe_id(task_id, "task_id")
        f = self._docs_tasks_dir(tenant_id) / f"{task_id}.json"
        if not f.exists():
            return None
        return DocsTask.model_validate_json(f.read_text())

    def update_docs_task(self, tenant_id: str, task_id: str, updates: dict) -> None:
        """Update docs task fields."""
        validate_safe_id(task_id, "task_id")
        f = self._docs_tasks_dir(tenant_id) / f"{task_id}.json"
        if not f.exists():
            raise ValueError(f"Docs task {task_id} not found")
        task = DocsTask.model_validate_json(f.read_text())
        task_dict = task.model_dump()
        task_dict.update(updates)
        updated = DocsTask.model_validate(task_dict)
        f.write_text(updated.model_dump_json(indent=2))
