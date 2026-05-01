"""Unit tests for the QuestionPlan model and related data structures.

Tests cover plan navigation, skip condition evaluation, state mutations,
and serialization — all pure logic with no LLM or AWS dependencies.
"""

import pytest

from src.models.interview_plan import (
    PlannedQuestion,
    PlannedQuestionLLM,
    QuestionPlan,
    QuestionPlanOutput,
    SkipCondition,
    TurnResponse,
    _evaluate_condition,
    _get_nested,
    _set_nested,
)


# ===================================================================
# Helper functions: _get_nested / _set_nested / _evaluate_condition
# ===================================================================


class TestGetNested:
    def test_single_level(self):
        assert _get_nested({"a": 1}, "a") == 1

    def test_deep_path(self):
        assert _get_nested({"a": {"b": {"c": 42}}}, "a.b.c") == 42

    def test_missing_key_returns_none(self):
        assert _get_nested({"a": 1}, "b") is None

    def test_missing_intermediate_returns_none(self):
        assert _get_nested({"a": 1}, "a.b.c") is None

    def test_empty_dict(self):
        assert _get_nested({}, "x") is None


class TestSetNested:
    def test_single_level(self):
        d: dict = {}
        _set_nested(d, "a", 1)
        assert d == {"a": 1}

    def test_deep_path_creates_intermediates(self):
        d: dict = {}
        _set_nested(d, "a.b.c", 42)
        assert d == {"a": {"b": {"c": 42}}}

    def test_overwrites_existing(self):
        d = {"a": {"b": 1}}
        _set_nested(d, "a.b", 2)
        assert d["a"]["b"] == 2


class TestEvaluateCondition:
    def test_eq_true(self):
        cond = SkipCondition(field_path="role", operator="eq", value="hub")
        assert _evaluate_condition(cond, {"role": "hub"}) is True

    def test_eq_false(self):
        cond = SkipCondition(field_path="role", operator="eq", value="hub")
        assert _evaluate_condition(cond, {"role": "spoke"}) is False

    def test_neq(self):
        cond = SkipCondition(field_path="role", operator="neq", value="hub")
        assert _evaluate_condition(cond, {"role": "spoke"}) is True

    def test_in_operator(self):
        cond = SkipCondition(field_path="protocol", operator="in", value=["bgp", "ospf"])
        assert _evaluate_condition(cond, {"protocol": "bgp"}) is True
        assert _evaluate_condition(cond, {"protocol": "static"}) is False

    def test_not_in_operator(self):
        cond = SkipCondition(field_path="protocol", operator="not_in", value=["bgp", "ospf"])
        assert _evaluate_condition(cond, {"protocol": "static"}) is True

    def test_exists(self):
        cond = SkipCondition(field_path="gpu_budget", operator="exists")
        assert _evaluate_condition(cond, {"gpu_budget": "moderate"}) is True
        assert _evaluate_condition(cond, {}) is False

    def test_not_exists(self):
        cond = SkipCondition(field_path="gpu_budget", operator="not_exists")
        assert _evaluate_condition(cond, {}) is True
        assert _evaluate_condition(cond, {"gpu_budget": "moderate"}) is False

    def test_nested_field(self):
        cond = SkipCondition(field_path="realtime-inference.model_size_category", operator="eq", value="large")
        assert _evaluate_condition(cond, {"realtime-inference": {"model_size_category": "large"}}) is True


# ===================================================================
# QuestionPlan — navigation
# ===================================================================


def _make_plan(*statuses: str) -> QuestionPlan:
    """Helper: build a plan with entries in the given statuses."""
    entries = []
    for i, status in enumerate(statuses):
        entries.append(PlannedQuestion(
            field_path=f"field_{i}",
            question_template=f"Question {i}?",
            status=status,
            is_blocking=(i < 2),
        ))
    return QuestionPlan(entries=entries)


class TestQuestionPlanNavigation:
    def test_current_question_returns_first_pending(self):
        plan = _make_plan("answered", "pending", "pending")
        assert plan.current_question().field_path == "field_1"

    def test_current_question_none_when_all_done(self):
        plan = _make_plan("answered", "skipped")
        assert plan.current_question() is None

    def test_next_question_returns_second_pending(self):
        plan = _make_plan("pending", "pending", "pending")
        assert plan.next_question().field_path == "field_1"

    def test_next_question_none_when_one_pending(self):
        plan = _make_plan("answered", "pending")
        assert plan.next_question() is None

    def test_pending_count(self):
        plan = _make_plan("answered", "pending", "pending", "skipped")
        assert plan.pending_count() == 2

    def test_blocking_complete_true_when_blocking_answered(self):
        # field_0 and field_1 are blocking (i < 2)
        plan = _make_plan("answered", "answered", "pending")
        assert plan.blocking_complete() is True

    def test_blocking_complete_false_when_blocking_pending(self):
        plan = _make_plan("answered", "pending", "pending")
        assert plan.blocking_complete() is False

    def test_all_missing_field_paths(self):
        plan = _make_plan("answered", "pending", "skipped", "pending")
        assert plan.all_missing_field_paths() == ["field_1", "field_3"]


# ===================================================================
# QuestionPlan — mutations
# ===================================================================


class TestQuestionPlanMutations:
    def test_mark_answered_sets_status_and_value(self):
        plan = _make_plan("pending")
        plan.mark_answered("field_0", "hub")
        assert plan.entries[0].status == "answered"
        assert plan.entries[0].answered_value == "hub"
        assert plan.populated_fields["field_0"] == "hub"

    def test_mark_answered_nested_field(self):
        plan = QuestionPlan(entries=[
            PlannedQuestion(field_path="realtime-inference.model_size_category", question_template="Model size?"),
        ])
        plan.mark_answered("realtime-inference.model_size_category", "large")
        assert plan.populated_fields == {"realtime-inference": {"model_size_category": "large"}}

    def test_mark_answered_only_affects_pending(self):
        plan = _make_plan("answered", "pending")
        plan.entries[0].answered_value = "original"
        plan.mark_answered("field_0", "new_value")
        # Already-answered entry should NOT be mutated
        assert plan.entries[0].answered_value == "original"

    def test_mark_skipped(self):
        plan = _make_plan("pending")
        plan.mark_skipped("field_0")
        assert plan.entries[0].status == "skipped"


# ===================================================================
# QuestionPlan — skip condition evaluation
# ===================================================================


class TestSkipConditionEvaluation:
    def test_evaluate_skip_conditions_skips_matching(self):
        plan = QuestionPlan(
            entries=[
                PlannedQuestion(
                    field_path="role",
                    question_template="What role?",
                    status="answered",
                    answered_value="spoke",
                ),
                PlannedQuestion(
                    field_path="hub_count",
                    question_template="How many hubs?",
                    skip_conditions=[
                        SkipCondition(field_path="role", operator="neq", value="hub"),
                    ],
                ),
            ],
            populated_fields={"role": "spoke"},
        )
        skipped = plan.evaluate_skip_conditions()
        assert "hub_count" in skipped
        assert plan.entries[1].status == "skipped"

    def test_evaluate_skip_conditions_keeps_non_matching(self):
        plan = QuestionPlan(
            entries=[
                PlannedQuestion(
                    field_path="role",
                    question_template="What role?",
                    status="answered",
                    answered_value="hub",
                ),
                PlannedQuestion(
                    field_path="hub_count",
                    question_template="How many hubs?",
                    skip_conditions=[
                        SkipCondition(field_path="role", operator="neq", value="hub"),
                    ],
                ),
            ],
            populated_fields={"role": "hub"},
        )
        skipped = plan.evaluate_skip_conditions()
        assert skipped == []
        assert plan.entries[1].status == "pending"

    def test_skip_ignores_already_answered(self):
        plan = QuestionPlan(
            entries=[
                PlannedQuestion(
                    field_path="already_done",
                    question_template="Done?",
                    status="answered",
                    skip_conditions=[
                        SkipCondition(field_path="x", operator="exists"),
                    ],
                ),
            ],
            populated_fields={"x": True},
        )
        skipped = plan.evaluate_skip_conditions()
        # Already answered — should not be re-skipped
        assert skipped == []

    def test_multiple_conditions_any_match_skips(self):
        plan = QuestionPlan(
            entries=[
                PlannedQuestion(
                    field_path="optional_field",
                    question_template="Optional?",
                    skip_conditions=[
                        SkipCondition(field_path="a", operator="eq", value="no"),
                        SkipCondition(field_path="b", operator="exists"),
                    ],
                ),
            ],
            populated_fields={"b": True},
        )
        skipped = plan.evaluate_skip_conditions()
        assert "optional_field" in skipped


# ===================================================================
# Serialization round-trip
# ===================================================================


class TestPlanSerialization:
    def test_round_trip_json(self):
        plan = QuestionPlan(
            entries=[
                PlannedQuestion(
                    field_path="role",
                    question_template="What role?",
                    expected_type="enum",
                    valid_values=["hub", "spoke"],
                    skip_conditions=[
                        SkipCondition(field_path="x", operator="eq", value="y"),
                    ],
                    status="answered",
                    answered_value="hub",
                ),
            ],
            auto_filled={"gpu_budget": "moderate"},
            auto_fill_rationale={"gpu_budget": "From seed data"},
            kb_summary="Test summary",
            populated_fields={"role": "hub", "gpu_budget": "moderate"},
        )
        json_str = plan.model_dump_json()
        restored = QuestionPlan.model_validate_json(json_str)
        assert restored.entries[0].field_path == "role"
        assert restored.entries[0].answered_value == "hub"
        assert restored.auto_filled["gpu_budget"] == "moderate"
        assert restored.kb_summary == "Test summary"


# ===================================================================
# LLM output models — basic construction
# ===================================================================


class TestLLMOutputModels:
    def test_question_plan_output(self):
        out = QuestionPlanOutput(
            auto_filled_fields={"gpu_budget": "high"},
            auto_fill_rationale={"gpu_budget": "From wizard"},
            questions=[
                PlannedQuestionLLM(field_path="model_size_category", question_template="Model size?"),
            ],
            kb_summary="Found inference deployment docs",
            initial_message="Welcome!",
        )
        assert len(out.questions) == 1
        assert out.initial_message == "Welcome!"

    def test_skip_conditions_single_dict_coerced_to_list(self):
        """LLMs often return a single dict instead of a list for skip_conditions."""
        q = PlannedQuestionLLM(
            field_path="hub_count",
            question_template="How many hubs?",
            skip_conditions={"field_path": "role", "operator": "neq", "value": "hub"},
        )
        assert isinstance(q.skip_conditions, list)
        assert len(q.skip_conditions) == 1
        assert q.skip_conditions[0].field_path == "role"

    def test_skip_conditions_list_passthrough(self):
        q = PlannedQuestionLLM(
            field_path="hub_count",
            question_template="How many hubs?",
            skip_conditions=[
                {"field_path": "role", "operator": "eq", "value": "spoke"},
            ],
        )
        assert len(q.skip_conditions) == 1

    def test_turn_response_defaults(self):
        resp = TurnResponse()
        assert resp.parsed_value is None
        assert resp.confidence == 0.0
        assert resp.deviation_detected is False

    def test_turn_response_confidence_bounds(self):
        resp = TurnResponse(confidence=1.0)
        assert resp.confidence == 1.0
        with pytest.raises(Exception):  # pydantic validation
            TurnResponse(confidence=1.5)
