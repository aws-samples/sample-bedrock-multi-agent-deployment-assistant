"""Tests for the service layer."""

import io
import zipfile
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Projects service
# ---------------------------------------------------------------------------


class TestProjectsService:
    def test_create_project_returns_dict(self, tmp_path):
        with patch("src.services.projects.get_store") as mock:
            mock_store = MagicMock()
            project_mock = MagicMock()
            project_mock.model_dump.return_value = {
                "project_id": "abc123",
                "name": "Test",
                "tenant_id": "t1",
                "mode": "wizard",
                "status": "REQUIREMENTS",
            }
            mock_store.create_project.return_value = project_mock
            mock.return_value = mock_store

            from src.services.projects import create_project_service

            result = create_project_service("t1", "Test")
            assert result["name"] == "Test"
            assert result["project_id"] == "abc123"
            mock_store.create_project.assert_called_once()

    def test_list_projects_returns_list(self):
        with patch("src.services.projects.get_store") as mock:
            p1 = MagicMock()
            p1.model_dump.return_value = {"project_id": "a", "name": "A"}
            p2 = MagicMock()
            p2.model_dump.return_value = {"project_id": "b", "name": "B"}
            mock.return_value.list_projects.return_value = [p1, p2]

            from src.services.projects import list_projects_service

            result = list_projects_service("t1")
            assert len(result) == 2
            assert result[0]["name"] == "A"

    def test_get_project_raises_value_error_if_not_found(self):
        with patch("src.services.projects.get_store") as mock:
            mock.return_value.get_project.return_value = None

            from src.services.projects import get_project_service

            with pytest.raises(ValueError, match="Project not found"):
                get_project_service("t1", "nonexistent")

    def test_delete_project_returns_status(self):
        with patch("src.services.projects.get_store") as mock:
            mock.return_value.delete_project.return_value = None

            from src.services.projects import delete_project_service

            result = delete_project_service("t1", "abc")
            assert result == {"status": "deleted"}

    def test_get_project_state_returns_full_state(self):
        with patch("src.services.projects.get_store") as mock:
            project_mock = MagicMock()
            project_mock.model_dump.return_value = {"project_id": "abc", "name": "Test"}
            mock.return_value.get_project.return_value = project_mock
            mock.return_value.load_step.side_effect = [
                {"use_case": "SD-WAN"},  # requirements
                {"options": []},          # design
                {"files": {}},            # iac
                None,                     # docs
            ]

            from src.services.projects import get_project_state_service

            result = get_project_state_service("t1", "abc")
            assert result["project"]["name"] == "Test"
            assert result["requirements"]["use_case"] == "SD-WAN"
            assert result["docs"] is None


# ---------------------------------------------------------------------------
# Export service
# ---------------------------------------------------------------------------


class TestExportService:
    def test_build_iac_zip_bytes(self):
        with patch("src.services.export.get_store") as mock:
            mock.return_value.load_step.return_value = {
                "files": {
                    "main.tf": 'resource "aws_instance" "web" {}',
                    "variables.tf": 'variable "region" {}',
                }
            }

            from src.services.export import build_iac_zip_bytes

            zip_bytes = build_iac_zip_bytes("t1", "abc")
            assert isinstance(zip_bytes, bytes)

            # Verify zip contents
            zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
            assert set(zf.namelist()) == {"main.tf", "variables.tf"}

    def test_build_iac_zip_raises_if_no_files(self):
        with patch("src.services.export.get_store") as mock:
            mock.return_value.load_step.return_value = None

            from src.services.export import build_iac_zip_bytes

            with pytest.raises(ValueError, match="No IaC files"):
                build_iac_zip_bytes("t1", "abc")

    def test_sanitize_zip_path_rejects_traversal(self):
        from src.services.export import _sanitize_zip_path

        with pytest.raises(ValueError, match="Path traversal"):
            _sanitize_zip_path("../../etc/passwd")

    def test_sanitize_zip_path_normalizes(self):
        from src.services.export import _sanitize_zip_path

        assert _sanitize_zip_path("/modules/networking/main.tf") == "modules/networking/main.tf"


# ---------------------------------------------------------------------------
# Design service
# ---------------------------------------------------------------------------


class TestDesignService:
    @patch("src.services.design.get_store")
    @patch("src.services.design._sanitize_and_rebuild_requirements", side_effect=lambda x: x)
    @patch("src.workers.local_worker.enqueue")
    def test_submit_design_local_async_returns_queued(self, mock_enqueue, mock_sanitize, mock_store):
        """submit_design_task enqueues to local worker when SQS is not configured."""
        with patch("src.services.design.settings") as mock_settings:
            mock_settings.sqs_design_queue_url = None

            from src.models.requirements import InterviewOutput, UseCases, RoutingProtocol
            from src.services.design import submit_design_task

            reqs = InterviewOutput(
                use_cases=[UseCases.SD_WAN],
                cloud_routing_protocol=RoutingProtocol.BGP,
                bandwidth=1000.0,
                compliance=["none"],
                solution_description="Deploy SD-WAN",
            )
            result = submit_design_task(reqs, project_id="p1", tenant_id="t1")
            assert result["status"] == "queued"
            assert "task_id" in result
            mock_enqueue.assert_called_once()
            # Verify enqueued body contains the right fields
            body = mock_enqueue.call_args[0][0]
            assert body["tenant_id"] == "t1"
            assert body["project_id"] == "p1"
            assert body["task_type"] == "design"

    @patch("src.services.design.get_store")
    @patch("src.services.design._sanitize_and_rebuild_requirements", side_effect=lambda x: x)
    @patch("src.workers.local_worker.enqueue")
    def test_submit_redesign_includes_feedback_in_body(self, mock_enqueue, mock_sanitize, mock_store):
        """submit_design_task includes feedback in the enqueued body for redesigns."""
        with patch("src.services.design.settings") as mock_settings:
            mock_settings.sqs_design_queue_url = None

            from src.models.requirements import InterviewOutput, UseCases, RoutingProtocol
            from src.services.design import submit_design_task

            reqs = InterviewOutput(
                use_cases=[UseCases.SD_WAN],
                cloud_routing_protocol=RoutingProtocol.BGP,
                bandwidth=1000.0,
                compliance=["none"],
                solution_description="Deploy SD-WAN",
            )
            result = submit_design_task(
                reqs,
                project_id="p1",
                tenant_id="t1",
                feedback="Need more HA",
            )
            assert result["status"] == "queued"
            body = mock_enqueue.call_args[0][0]
            assert body["feedback"] == "Need more HA"
            assert body["task_type"] == "redesign"
