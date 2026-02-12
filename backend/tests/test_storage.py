"""Integration tests for LocalProjectStore.

Tests the file-based storage backend directly using temp directories.
No mocking of the store itself — only DATA_DIR is patched to use tmp_path.
"""

import json
from unittest.mock import patch

import pytest

from src.models.project import ProjectStatus
from src.storage.local import LocalProjectStore


@pytest.fixture
def store(tmp_path):
    """Create a LocalProjectStore backed by a temp directory."""
    with patch("src.storage.local.DATA_DIR", tmp_path):
        yield LocalProjectStore()


# ---------------------------------------------------------------------------
# create_project
# ---------------------------------------------------------------------------


def test_create_project(store, tmp_path):
    with patch("src.storage.local.DATA_DIR", tmp_path):
        project = store.create_project("tenant1", "proj1", "Test Project")
        assert project.tenant_id == "tenant1"
        assert project.project_id == "proj1"
        assert project.name == "Test Project"
        assert project.status == ProjectStatus.REQUIREMENTS
        assert project.current_step == "requirements"


def test_create_project_persists_to_disk(store, tmp_path):
    with patch("src.storage.local.DATA_DIR", tmp_path):
        store.create_project("tenant1", "proj1", "Persisted")
        meta_file = tmp_path / "tenant1" / "proj1" / "project.json"
        assert meta_file.exists()
        data = json.loads(meta_file.read_text())
        assert data["name"] == "Persisted"


# ---------------------------------------------------------------------------
# get_project
# ---------------------------------------------------------------------------


def test_get_project(store, tmp_path):
    with patch("src.storage.local.DATA_DIR", tmp_path):
        store.create_project("tenant1", "proj1", "Test")
        result = store.get_project("tenant1", "proj1")
        assert result is not None
        assert result.name == "Test"


def test_get_project_not_found(store, tmp_path):
    with patch("src.storage.local.DATA_DIR", tmp_path):
        result = store.get_project("tenant1", "nonexistent")
        assert result is None


# ---------------------------------------------------------------------------
# list_projects
# ---------------------------------------------------------------------------


def test_list_projects(store, tmp_path):
    with patch("src.storage.local.DATA_DIR", tmp_path):
        store.create_project("tenant1", "proj1", "First")
        store.create_project("tenant1", "proj2", "Second")
        projects = store.list_projects("tenant1")
        assert len(projects) == 2
        names = {p.name for p in projects}
        assert names == {"First", "Second"}


def test_list_projects_empty_tenant(store, tmp_path):
    with patch("src.storage.local.DATA_DIR", tmp_path):
        projects = store.list_projects("empty_tenant")
        assert projects == []


def test_list_projects_tenant_isolation(store, tmp_path):
    with patch("src.storage.local.DATA_DIR", tmp_path):
        store.create_project("tenant1", "proj1", "T1 Project")
        store.create_project("tenant2", "proj1", "T2 Project")
        t1_projects = store.list_projects("tenant1")
        t2_projects = store.list_projects("tenant2")
        assert len(t1_projects) == 1
        assert len(t2_projects) == 1
        assert t1_projects[0].name == "T1 Project"
        assert t2_projects[0].name == "T2 Project"


# ---------------------------------------------------------------------------
# delete_project
# ---------------------------------------------------------------------------


def test_delete_project(store, tmp_path):
    with patch("src.storage.local.DATA_DIR", tmp_path):
        store.create_project("tenant1", "proj1", "To Delete")
        store.delete_project("tenant1", "proj1")
        result = store.get_project("tenant1", "proj1")
        assert result is None


def test_delete_project_removes_directory(store, tmp_path):
    with patch("src.storage.local.DATA_DIR", tmp_path):
        store.create_project("tenant1", "proj1", "Doomed")
        project_dir = tmp_path / "tenant1" / "proj1"
        assert project_dir.exists()
        store.delete_project("tenant1", "proj1")
        assert not project_dir.exists()


def test_delete_nonexistent_project_no_error(store, tmp_path):
    """Deleting a project that does not exist should not raise."""
    with patch("src.storage.local.DATA_DIR", tmp_path):
        store.delete_project("tenant1", "ghost")  # should not raise


# ---------------------------------------------------------------------------
# update_project
# ---------------------------------------------------------------------------


def test_update_project(store, tmp_path):
    with patch("src.storage.local.DATA_DIR", tmp_path):
        project = store.create_project("tenant1", "proj1", "Original")
        project.name = "Updated"
        store.update_project(project)
        result = store.get_project("tenant1", "proj1")
        assert result.name == "Updated"


def test_update_project_changes_updated_at(store, tmp_path):
    with patch("src.storage.local.DATA_DIR", tmp_path):
        project = store.create_project("tenant1", "proj1", "Test")
        original_updated = project.updated_at
        project.name = "Changed"
        store.update_project(project)
        result = store.get_project("tenant1", "proj1")
        # updated_at should have been refreshed
        assert result.updated_at >= original_updated


# ---------------------------------------------------------------------------
# save_step / load_step
# ---------------------------------------------------------------------------


def test_save_and_load_step(store, tmp_path):
    with patch("src.storage.local.DATA_DIR", tmp_path):
        store.create_project("tenant1", "proj1", "Test")
        data = {"business_goals": "Secure deployment", "use_case": "east-west"}
        store.save_step("tenant1", "proj1", "requirements", data)
        loaded = store.load_step("tenant1", "proj1", "requirements")
        assert loaded == data


def test_save_step_advances_status(store, tmp_path):
    with patch("src.storage.local.DATA_DIR", tmp_path):
        store.create_project("tenant1", "proj1", "Test")

        store.save_step("tenant1", "proj1", "requirements", {"data": "test"})
        project = store.get_project("tenant1", "proj1")
        assert project.status == ProjectStatus.DESIGN

        store.save_step("tenant1", "proj1", "design", {"data": "test"})
        project = store.get_project("tenant1", "proj1")
        assert project.status == ProjectStatus.IAC

        store.save_step("tenant1", "proj1", "iac", {"data": "test"})
        project = store.get_project("tenant1", "proj1")
        assert project.status == ProjectStatus.DOCUMENTATION

        store.save_step("tenant1", "proj1", "docs", {"data": "test"})
        project = store.get_project("tenant1", "proj1")
        assert project.status == ProjectStatus.COMPLETE


def test_save_step_advances_current_step(store, tmp_path):
    with patch("src.storage.local.DATA_DIR", tmp_path):
        store.create_project("tenant1", "proj1", "Test")

        store.save_step("tenant1", "proj1", "requirements", {})
        project = store.get_project("tenant1", "proj1")
        assert project.current_step == "design"

        store.save_step("tenant1", "proj1", "design", {})
        project = store.get_project("tenant1", "proj1")
        assert project.current_step == "iac"

        store.save_step("tenant1", "proj1", "iac", {})
        project = store.get_project("tenant1", "proj1")
        assert project.current_step == "documentation"


def test_save_step_invalid_step(store, tmp_path):
    with patch("src.storage.local.DATA_DIR", tmp_path):
        store.create_project("tenant1", "proj1", "Test")
        with pytest.raises(ValueError, match="Invalid step"):
            store.save_step("tenant1", "proj1", "invalid_step", {})


def test_load_step_not_found(store, tmp_path):
    with patch("src.storage.local.DATA_DIR", tmp_path):
        store.create_project("tenant1", "proj1", "Test")
        result = store.load_step("tenant1", "proj1", "requirements")
        assert result is None


def test_load_step_invalid_step(store, tmp_path):
    with patch("src.storage.local.DATA_DIR", tmp_path):
        result = store.load_step("tenant1", "proj1", "invalid")
        assert result is None


def test_save_step_persists_json_file(store, tmp_path):
    with patch("src.storage.local.DATA_DIR", tmp_path):
        store.create_project("tenant1", "proj1", "Test")
        store.save_step("tenant1", "proj1", "design", {"options": [1, 2, 3]})
        step_file = tmp_path / "tenant1" / "proj1" / "design.json"
        assert step_file.exists()
        assert json.loads(step_file.read_text()) == {"options": [1, 2, 3]}


def test_save_step_overwrite(store, tmp_path):
    """Saving the same step twice should overwrite the previous data."""
    with patch("src.storage.local.DATA_DIR", tmp_path):
        store.create_project("tenant1", "proj1", "Test")
        store.save_step("tenant1", "proj1", "requirements", {"version": 1})
        store.save_step("tenant1", "proj1", "requirements", {"version": 2})
        loaded = store.load_step("tenant1", "proj1", "requirements")
        assert loaded == {"version": 2}


# ---------------------------------------------------------------------------
# All valid steps
# ---------------------------------------------------------------------------


def test_all_valid_steps_roundtrip(store, tmp_path):
    """Verify that all four valid steps can be saved and loaded."""
    with patch("src.storage.local.DATA_DIR", tmp_path):
        store.create_project("tenant1", "proj1", "Test")
        for step in ("requirements", "design", "iac", "docs"):
            payload = {"step": step, "content": f"data for {step}"}
            store.save_step("tenant1", "proj1", step, payload)
            loaded = store.load_step("tenant1", "proj1", step)
            assert loaded == payload
