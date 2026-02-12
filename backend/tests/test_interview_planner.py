"""Unit tests for interview_planner — plan generation, enrichment, KB helpers, fallback.

All tests mock Bedrock and KB search so they run without AWS credentials.
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

from src.models.interview_plan import (
    PlannedQuestion,
    PlannedQuestionLLM,
    QuestionPlan,
    QuestionPlanOutput,
)
from src.models.requirements import UseCases
from src.tools.kb_search import KBResult


@pytest.fixture(autouse=True)
def _mock_planner_imports():
    with patch("strands.models.bedrock.BedrockModel", MagicMock()), \
         patch("strands.Agent", MagicMock()):
        mods = [k for k in sys.modules if "interview_planner" in k]
        for m in mods:
            del sys.modules[m]
        yield


# ===================================================================
# _format_kb_results
# ===================================================================


class TestFormatKBResults:
    def test_empty_results(self):
        from src.agents.interview_planner import _format_kb_results

        assert "No knowledge base" in _format_kb_results([])

    def test_formats_results(self):
        from src.agents.interview_planner import _format_kb_results

        results = [
            KBResult(text="Hub-spoke guide", source_uri="s3://b/doc.pdf", score=0.92),
            KBResult(text="Sizing info", source_uri="s3://b/sizing.pdf", score=0.85),
        ]
        formatted = _format_kb_results(results)
        assert "Hub-spoke guide" in formatted
        assert "0.92" in formatted
        assert "---" in formatted


# ===================================================================
# _enrich_plan
# ===================================================================


class TestEnrichPlan:
    def test_marks_optional_from_registry(self):
        from src.agents.interview_planner import _enrich_plan

        output = QuestionPlanOutput(
            questions=[
                PlannedQuestionLLM(field_path="sd-wan.role", question_template="Role?"),
                PlannedQuestionLLM(field_path="sd-wan.performance", question_template="Perf?"),
            ],
            initial_message="Hello!",
        )
        plan = _enrich_plan(output, [UseCases.SD_WAN], {})
        role_entry = next(e for e in plan.entries if e.field_path == "sd-wan.role")
        perf_entry = next(e for e in plan.entries if e.field_path == "sd-wan.performance")

        # "performance" is optional for SD-WAN in the registry
        assert perf_entry.is_optional is True
        assert perf_entry.is_blocking is False
        # "role" is required
        assert role_entry.is_optional is False
        assert role_entry.is_blocking is True

    def test_preserves_auto_filled(self):
        from src.agents.interview_planner import _enrich_plan

        output = QuestionPlanOutput(
            auto_filled_fields={"bandwidth": 5000},
            auto_fill_rationale={"bandwidth": "From wizard"},
            questions=[],
            initial_message="Ready!",
        )
        seed = {"use_cases": ["sd-wan"]}
        plan = _enrich_plan(output, [UseCases.SD_WAN], seed)
        assert plan.auto_filled == {"bandwidth": 5000}
        assert plan.populated_fields["bandwidth"] == 5000
        assert plan.populated_fields["use_cases"] == ["sd-wan"]

    def test_base_optional_fields(self):
        from src.agents.interview_planner import _enrich_plan

        output = QuestionPlanOutput(
            questions=[
                PlannedQuestionLLM(field_path="compliance", question_template="Compliance?"),
                PlannedQuestionLLM(field_path="user_info", question_template="Name?"),
            ],
            initial_message="Hi!",
        )
        plan = _enrich_plan(output, [], {})
        for entry in plan.entries:
            assert entry.is_optional is True
            assert entry.is_blocking is False


# ===================================================================
# _fallback_plan
# ===================================================================


class TestFallbackPlan:
    def test_generates_entries_from_schema(self):
        from src.agents.interview_planner import _fallback_plan

        plan = _fallback_plan([UseCases.SD_WAN], {"use_cases": ["sd-wan"]}, None)
        assert len(plan.entries) > 0
        # Should have field paths from the schema
        paths = [e.field_path for e in plan.entries]
        assert len(paths) > 0
        # Seed data should be in populated_fields
        assert plan.populated_fields.get("use_cases") == ["sd-wan"]

    def test_fallback_has_no_kb_context(self):
        from src.agents.interview_planner import _fallback_plan

        plan = _fallback_plan([UseCases.EGRESS], {}, None)
        for entry in plan.entries:
            assert entry.kb_context == ""
            assert entry.skip_conditions == []


# ===================================================================
# _search_kb_for_planning / _search_kb_for_replan
# ===================================================================


class TestKBSearchHelpers:
    @patch("src.agents.interview_planner.kb_search_filtered")
    def test_search_kb_for_planning_queries_per_use_case(self, mock_kb):
        from src.agents.interview_planner import _search_kb_for_planning

        mock_kb.return_value = [KBResult(text="result", source_uri="s3://b/d.pdf", score=0.9)]
        results = _search_kb_for_planning(
            [UseCases.SD_WAN, UseCases.INSPECTION],
            {"solution_description": "test"},
        )
        # Should call kb_search_filtered once per non-NOTKNOWN use case
        assert mock_kb.call_count == 2
        assert len(results) == 2

    @patch("src.agents.interview_planner.kb_search_filtered")
    def test_search_kb_for_planning_skips_notknown(self, mock_kb):
        from src.agents.interview_planner import _search_kb_for_planning

        mock_kb.return_value = []
        _search_kb_for_planning([UseCases.NOTKNOWN], {})
        mock_kb.assert_not_called()

    @patch("src.agents.interview_planner.kb_search_filtered")
    def test_search_kb_for_replan_passes_deployment_type(self, mock_kb):
        from src.agents.interview_planner import _search_kb_for_replan

        mock_kb.return_value = []
        _search_kb_for_replan([UseCases.SD_WAN], "User wants multi-region", "hub-spoke")
        call_kwargs = mock_kb.call_args[1]
        assert call_kwargs["deployment_type"] == "hub-spoke"


# ===================================================================
# generate_plan (mocked LLM)
# ===================================================================


class TestGeneratePlan:
    @patch("src.agents.interview_planner.kb_search_filtered")
    @patch("src.agents.interview_planner._invoke_planner")
    @patch("src.agents.interview_planner.create_bedrock_model")
    def test_generate_plan_returns_plan_and_message(self, mock_model, mock_invoke, mock_kb):
        from src.agents.interview_planner import generate_plan

        mock_kb.return_value = []
        mock_result = MagicMock()
        mock_result.structured_output = QuestionPlanOutput(
            auto_filled_fields={"bandwidth": 1000},
            auto_fill_rationale={"bandwidth": "From seed"},
            questions=[
                PlannedQuestionLLM(
                    field_path="sd-wan.role",
                    question_template="What role should the FortiGate play?",
                    expected_type="enum",
                    valid_values=["hub", "spoke"],
                ),
            ],
            kb_summary="Found architecture docs",
            initial_message="Welcome! I see you need SD-WAN.",
        )
        mock_invoke.return_value = mock_result

        with patch("src.config.metrics.metrics", MagicMock()):
            plan, message = generate_plan(
                seed_data={"use_cases": ["sd-wan"]},
                use_cases=[UseCases.SD_WAN],
            )

        assert isinstance(plan, QuestionPlan)
        assert message == "Welcome! I see you need SD-WAN."
        assert plan.entries[0].field_path == "sd-wan.role"
        assert plan.auto_filled["bandwidth"] == 1000

    @patch("src.agents.interview_planner.kb_search_filtered")
    @patch("src.agents.interview_planner._invoke_planner")
    @patch("src.agents.interview_planner.create_bedrock_model")
    def test_generate_plan_falls_back_on_bad_output(self, mock_model, mock_invoke, mock_kb):
        from src.agents.interview_planner import generate_plan

        mock_kb.return_value = []
        mock_result = MagicMock()
        mock_result.structured_output = "not a QuestionPlanOutput"
        mock_invoke.return_value = mock_result

        with patch("src.config.metrics.metrics", MagicMock()):
            plan, message = generate_plan(
                seed_data={},
                use_cases=[UseCases.SD_WAN],
            )

        assert isinstance(plan, QuestionPlan)
        assert len(plan.entries) > 0  # fallback plan has entries
        assert message == ""


# ===================================================================
# replan (mocked LLM)
# ===================================================================


class TestReplan:
    @patch("src.agents.interview_planner.kb_search_filtered")
    @patch("src.agents.interview_planner._invoke_planner")
    @patch("src.agents.interview_planner.create_bedrock_model")
    def test_replan_preserves_answered_entries(self, mock_model, mock_invoke, mock_kb):
        from src.agents.interview_planner import replan

        mock_kb.return_value = []
        mock_result = MagicMock()
        mock_result.structured_output = QuestionPlanOutput(
            questions=[
                PlannedQuestionLLM(field_path="new_field", question_template="New question?"),
            ],
            initial_message="Updated plan!",
        )
        mock_invoke.return_value = mock_result

        # Existing plan with one answered entry
        current = QuestionPlan(
            entries=[
                PlannedQuestion(field_path="role", question_template="Role?", status="answered", answered_value="hub"),
                PlannedQuestion(field_path="old_pending", question_template="Old?", status="pending"),
            ],
            populated_fields={"role": "hub"},
        )

        with patch("src.config.metrics.metrics", MagicMock()):
            new_plan, msg = replan(current, "User wants multi-region", [UseCases.SD_WAN])

        assert msg == "Updated plan!"
        # Answered entry is preserved
        answered = [e for e in new_plan.entries if e.status == "answered"]
        assert len(answered) == 1
        assert answered[0].field_path == "role"
        # New entry is added
        pending = [e for e in new_plan.entries if e.status == "pending"]
        assert any(e.field_path == "new_field" for e in pending)
        assert new_plan.replanned_count == 1

    @patch("src.agents.interview_planner.kb_search_filtered")
    @patch("src.agents.interview_planner._invoke_planner")
    @patch("src.agents.interview_planner.create_bedrock_model")
    def test_replan_fallback_keeps_current(self, mock_model, mock_invoke, mock_kb):
        from src.agents.interview_planner import replan

        mock_kb.return_value = []
        mock_result = MagicMock()
        mock_result.structured_output = "bad output"
        mock_invoke.return_value = mock_result

        current = QuestionPlan(
            entries=[PlannedQuestion(field_path="role", question_template="Role?")],
            populated_fields={},
        )

        with patch("src.config.metrics.metrics", MagicMock()):
            result_plan, msg = replan(current, "deviation", [UseCases.SD_WAN])

        # Fallback: returns current plan unchanged
        assert result_plan is current
        assert "noted" in msg.lower()
