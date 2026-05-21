"""Tests for the local async worker and shared design processing module."""

import time
from unittest.mock import MagicMock, patch

import pytest

from src.models.requirements import InterviewOutput, UseCases


# ---------------------------------------------------------------------------
# Shared processing: build_agent_prompt
# ---------------------------------------------------------------------------


class TestBuildAgentPrompt:
    def test_basic_prompt_contains_requirements(self):
        from src.services.design_processing import build_agent_prompt

        reqs = InterviewOutput(
            use_cases=["realtime-inference"],
            gpu_budget="moderate",
            availability_requirement="production-single-az",
            data_sensitivity="internal",
            compliance=["none"],
            solution_description="Deploy real-time inference",
        )
        prompt = build_agent_prompt(reqs)
        assert "RequirementsDocument" in prompt
        assert "realtime-inference" in prompt.lower()

    def test_prompt_includes_feedback(self):
        from src.services.design_processing import build_agent_prompt

        reqs = InterviewOutput(
            use_cases=["realtime-inference"],
            gpu_budget="moderate",
            availability_requirement="production-single-az",
            data_sensitivity="internal",
            compliance=["none"],
            solution_description="Deploy real-time inference",
        )
        prompt = build_agent_prompt(reqs, feedback="Need more HA")
        assert "Need more HA" in prompt
        assert "User Feedback" in prompt

    def test_prompt_includes_previous_options(self):
        from src.models.design import (
            DesignOption, ApplianceBlueprint, InterfaceBlueprint,
            KBReference, VPCBlueprint,
        )
        from src.services.design_processing import build_agent_prompt

        reqs = InterviewOutput(
            use_cases=["realtime-inference"],
            gpu_budget="moderate",
            availability_requirement="production-single-az",
            data_sensitivity="internal",
            compliance=["none"],
            solution_description="Deploy real-time inference",
        )
        option = DesignOption(
            name="Prev Option",
            description="Previous",
            architecture_summary="Single FGT",
            pros=["Simple", "Fast"],
            cons=["No HA", "Limited"],
            estimated_monthly_cost_usd=100.0,
            security_posture_rating=2,
            complexity_rating=1,
            deployment_pattern="standalone",
            use_case="realtime-inference",
            ha_mode="none",
            appliance_instance_type="g5.xlarge",
            aws_services=["VPC"],
            vpc_topology=[VPCBlueprint(role="security", subnet_roles=["public"], availability_zones=1)],
            appliance_topology=[ApplianceBlueprint(
                role="active", vpc_role="security",
                interfaces=[InterfaceBlueprint(port_name="port1", subnet_role="public", description="WAN")],
            )],
            kb_references=[KBReference(source_uri="s3://kb/doc.md", excerpt="Guide", relevance_score=0.9)],
        )
        prompt = build_agent_prompt(reqs, previous_options=[option])
        assert "Previous Design Options" in prompt
        assert "Prev Option" in prompt


# ---------------------------------------------------------------------------
# Shared processing: extract_recommendation
# ---------------------------------------------------------------------------


class TestExtractRecommendation:
    def test_extracts_from_structured_output(self):
        from src.models.design import (
            DesignOption, DesignRecommendation, ApplianceBlueprint,
            InterfaceBlueprint, KBReference, VPCBlueprint,
        )
        from src.services.design_processing import extract_recommendation

        option = DesignOption(
            name="Option A",
            description="Test",
            architecture_summary="Single GPU instance",
            pros=["Simple", "Fast"],
            cons=["No HA", "Limited"],
            estimated_monthly_cost_usd=100.0,
            security_posture_rating=2,
            complexity_rating=1,
            deployment_pattern="standalone",
            use_case="realtime-inference",
            ha_mode="none",
            appliance_instance_type="g5.xlarge",
            aws_services=["VPC"],
            vpc_topology=[VPCBlueprint(role="security", subnet_roles=["public"], availability_zones=1)],
            appliance_topology=[ApplianceBlueprint(
                role="active", vpc_role="security",
                interfaces=[InterfaceBlueprint(port_name="port1", subnet_role="public", description="WAN")],
            )],
            kb_references=[KBReference(source_uri="s3://kb/doc.md", excerpt="Guide", relevance_score=0.9)],
        )
        options = [option.model_copy(update={"name": f"Option {c}"}) for c in "ABC"]
        rec = DesignRecommendation(
            options=options,
            recommended_option_index=0,
            rationale="test",
            requirements_summary="test",
        )

        mock_result = MagicMock()
        mock_result.structured_output = rec

        extracted = extract_recommendation(mock_result, ["s3://templates/a"])
        assert isinstance(extracted, DesignRecommendation)
        assert extracted.available_templates == ["s3://templates/a"]
        assert len(extracted.options) == 3


# ---------------------------------------------------------------------------
# Local worker: enqueue + process
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestLocalWorker:
    @patch("src.workers.local_worker.process_design_task")
    def test_enqueue_and_process(self, mock_process):
        """Worker processes an enqueued task."""
        from src.workers.local_worker import _notify_local, enqueue, shutdown, startup

        startup()
        body = {"task_id": "test-1", "tenant_id": "t1", "project_id": "p1"}
        enqueue(body)

        # Give the worker thread time to process
        time.sleep(0.5)
        shutdown(timeout=5.0)

        mock_process.assert_called_once_with(body, notify_fn=_notify_local)

    @patch("src.workers.local_worker.process_design_task")
    def test_graceful_shutdown(self, mock_process):
        """Worker thread stops cleanly on shutdown."""
        from src.workers.local_worker import shutdown, startup

        startup()
        shutdown(timeout=5.0)

        from src.workers import local_worker
        assert local_worker._thread is None

    @patch("src.workers.local_worker.process_design_task", side_effect=RuntimeError("boom"))
    @patch("src.workers.local_worker.mark_task_failed")
    def test_failed_task_is_marked(self, mock_mark_failed, mock_process):
        """Worker marks task as failed on exception and continues."""
        from src.workers.local_worker import _notify_local, enqueue, shutdown, startup

        startup()
        body = {"task_id": "fail-1", "tenant_id": "t1", "project_id": "p1"}
        enqueue(body)

        time.sleep(0.5)
        shutdown(timeout=5.0)

        mock_mark_failed.assert_called_once_with(body, notify_fn=_notify_local)
