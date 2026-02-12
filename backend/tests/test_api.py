"""Integration tests for FastAPI API endpoints.

Tests the REST API layer using FastAPI's TestClient (synchronous, backed by httpx).
Storage is pointed at a temp directory so no real data is touched.
No AWS credentials or LLM calls are required.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.storage import get_store


@pytest.fixture(autouse=True)
def _clear_store_cache():
    """Clear the cached get_store() singleton between tests."""
    get_store.cache_clear()
    yield
    get_store.cache_clear()


@pytest.fixture
def client(tmp_path):
    """Create a test client whose storage writes to a temp directory."""
    with patch("src.storage.local.DATA_DIR", tmp_path):
        from src.main import app

        yield TestClient(app)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


def test_health_check(client):
    response = client.get("/ping")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# POST /api/projects
# ---------------------------------------------------------------------------


def test_create_project(client):
    response = client.post(
        "/api/projects?tenant_id=test-tenant",
        json={"name": "Test Project"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Test Project"
    assert data["tenant_id"] == "test-tenant"
    assert "project_id" in data
    assert data["status"] == "requirements"


def test_create_project_default_tenant(client):
    response = client.post("/api/projects", json={"name": "Default Tenant"})
    assert response.status_code == 200
    assert response.json()["tenant_id"] == "default"


# ---------------------------------------------------------------------------
# GET /api/projects
# ---------------------------------------------------------------------------


def test_list_projects(client):
    client.post("/api/projects?tenant_id=t1", json={"name": "Project A"})
    client.post("/api/projects?tenant_id=t1", json={"name": "Project B"})

    response = client.get("/api/projects?tenant_id=t1")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2


def test_list_projects_empty(client):
    response = client.get("/api/projects?tenant_id=empty")
    assert response.status_code == 200
    assert response.json() == []


def test_list_projects_tenant_isolation(client):
    client.post("/api/projects?tenant_id=t1", json={"name": "T1 Project"})
    client.post("/api/projects?tenant_id=t2", json={"name": "T2 Project"})

    response_t1 = client.get("/api/projects?tenant_id=t1")
    response_t2 = client.get("/api/projects?tenant_id=t2")
    assert len(response_t1.json()) == 1
    assert len(response_t2.json()) == 1
    assert response_t1.json()[0]["name"] == "T1 Project"
    assert response_t2.json()[0]["name"] == "T2 Project"


# ---------------------------------------------------------------------------
# GET /api/projects/{project_id}
# ---------------------------------------------------------------------------


def test_get_project(client):
    create_resp = client.post(
        "/api/projects?tenant_id=t1", json={"name": "Test"}
    )
    project_id = create_resp.json()["project_id"]

    response = client.get(f"/api/projects/{project_id}?tenant_id=t1")
    assert response.status_code == 200
    assert response.json()["name"] == "Test"


def test_get_project_not_found(client):
    response = client.get("/api/projects/nonexistent?tenant_id=t1")
    assert response.status_code == 404


def test_get_project_wrong_tenant(client):
    """A project created under tenant t1 should not be visible under t2."""
    create_resp = client.post(
        "/api/projects?tenant_id=t1", json={"name": "T1 Only"}
    )
    project_id = create_resp.json()["project_id"]

    response = client.get(f"/api/projects/{project_id}?tenant_id=t2")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /api/projects/{project_id}
# ---------------------------------------------------------------------------


def test_delete_project(client):
    create_resp = client.post(
        "/api/projects?tenant_id=t1", json={"name": "Test"}
    )
    project_id = create_resp.json()["project_id"]

    delete_resp = client.delete(f"/api/projects/{project_id}?tenant_id=t1")
    assert delete_resp.status_code == 200
    assert delete_resp.json() == {"status": "deleted"}

    get_resp = client.get(f"/api/projects/{project_id}?tenant_id=t1")
    assert get_resp.status_code == 404


def test_delete_project_idempotent(client):
    """Deleting a nonexistent project should still return 200 (no-op)."""
    response = client.delete("/api/projects/ghost?tenant_id=t1")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# GET /api/projects/{project_id}/state
# ---------------------------------------------------------------------------


def test_get_project_state(client):
    create_resp = client.post(
        "/api/projects?tenant_id=t1", json={"name": "Test"}
    )
    project_id = create_resp.json()["project_id"]

    response = client.get(f"/api/projects/{project_id}/state?tenant_id=t1")
    assert response.status_code == 200
    data = response.json()
    assert "project" in data
    assert data["project"]["name"] == "Test"
    assert data["requirements"] is None
    assert data["design"] is None
    assert data["iac"] is None
    assert data["docs"] is None


def test_get_project_state_with_step_data(client, tmp_path):
    """After saving step data via the store, /state should reflect it."""
    create_resp = client.post(
        "/api/projects?tenant_id=t1", json={"name": "Stateful"}
    )
    project_id = create_resp.json()["project_id"]

    # Directly save step data through the store
    with patch("src.storage.local.DATA_DIR", tmp_path):
        store = get_store()
        store.save_step("t1", project_id, "requirements", {"use_case": "sd-wan"})

    response = client.get(f"/api/projects/{project_id}/state?tenant_id=t1")
    assert response.status_code == 200
    data = response.json()
    assert data["requirements"] == {"use_case": "sd-wan"}
    assert data["project"]["status"] == "design"  # status advanced


def test_get_project_state_not_found(client):
    response = client.get("/api/projects/nonexistent/state?tenant_id=t1")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/export/{project_id}/iac.zip
# ---------------------------------------------------------------------------


def test_export_iac_not_found(client):
    """Export should 404 when there is no IaC data for the project."""
    response = client.get("/api/export/nonexistent/iac.zip?tenant_id=t1")
    assert response.status_code == 404


def test_export_iac_no_files_key(client, tmp_path):
    """Export should 404 when IaC data exists but has no 'files' key."""
    create_resp = client.post(
        "/api/projects?tenant_id=t1", json={"name": "NoFiles"}
    )
    project_id = create_resp.json()["project_id"]

    with patch("src.storage.local.DATA_DIR", tmp_path):
        store = get_store()
        store.save_step("t1", project_id, "iac", {"validation": "passed"})

    response = client.get(f"/api/export/{project_id}/iac.zip?tenant_id=t1")
    assert response.status_code == 404


def test_export_iac_zip_success(client, tmp_path):
    """Export should return a zip when IaC files exist."""
    import io
    import zipfile

    create_resp = client.post(
        "/api/projects?tenant_id=t1", json={"name": "WithIaC"}
    )
    project_id = create_resp.json()["project_id"]

    iac_data = {
        "files": {
            "main.tf": 'resource "aws_instance" "fgt" {}',
            "variables.tf": 'variable "region" { default = "us-east-1" }',
        }
    }
    with patch("src.storage.local.DATA_DIR", tmp_path):
        store = get_store()
        store.save_step("t1", project_id, "iac", iac_data)

    response = client.get(f"/api/export/{project_id}/iac.zip?tenant_id=t1")
    assert response.status_code == 200
    assert "application/zip" in response.headers["content-type"]

    # Verify zip contents
    zf = zipfile.ZipFile(io.BytesIO(response.content))
    assert set(zf.namelist()) == {"main.tf", "variables.tf"}
    assert zf.read("main.tf").decode() == 'resource "aws_instance" "fgt" {}'


# ---------------------------------------------------------------------------
# Full create-to-state roundtrip
# ---------------------------------------------------------------------------


def test_full_project_lifecycle(client, tmp_path):
    """Create project, save all steps, verify final state is complete."""
    create_resp = client.post(
        "/api/projects?tenant_id=t1", json={"name": "Lifecycle"}
    )
    assert create_resp.status_code == 200
    project_id = create_resp.json()["project_id"]

    with patch("src.storage.local.DATA_DIR", tmp_path):
        store = get_store()
        store.save_step("t1", project_id, "requirements", {"use_case": "egress"})
        store.save_step("t1", project_id, "design", {"options": [{"name": "A"}]})
        store.save_step(
            "t1",
            project_id,
            "iac",
            {"files": {"main.tf": "# terraform"}},
        )
        store.save_step("t1", project_id, "docs", {"guide": "# Implementation"})

    state_resp = client.get(f"/api/projects/{project_id}/state?tenant_id=t1")
    assert state_resp.status_code == 200
    state = state_resp.json()

    assert state["project"]["status"] == "complete"
    assert state["requirements"]["use_case"] == "egress"
    assert state["design"]["options"][0]["name"] == "A"
    assert "main.tf" in state["iac"]["files"]
    assert state["docs"]["guide"] == "# Implementation"
