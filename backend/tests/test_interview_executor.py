"""Unit tests for interview_executor — value validation, prompt building, turn execution.

Value validation tests are pure logic. Turn execution tests mock the Bedrock agent.
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

from src.models.interview_plan import PlannedQuestion, QuestionPlan, TurnResponse


# ===================================================================
# _validate_parsed_value — pure logic (no LLM)
# ===================================================================


# We need to mock BedrockModel before importing executor since it reads a prompt at import time
@pytest.fixture(autouse=True)
def _mock_executor_imports():
    with patch("strands.models.bedrock.BedrockModel", MagicMock()), \
         patch("strands.Agent", MagicMock()):
        mods = [k for k in sys.modules if "interview_executor" in k]
        for m in mods:
            del sys.modules[m]
        yield


def _make_question(**kwargs) -> PlannedQuestion:
    defaults = {
        "field_path": "test_field",
        "question_template": "Test?",
        "expected_type": "str",
    }
    defaults.update(kwargs)
    return PlannedQuestion(**defaults)


class TestValidateParsedValue:
    def test_none_value_passthrough(self):
        from src.agents.interview_executor import _validate_parsed_value

        q = _make_question(expected_type="int")
        resp = TurnResponse(parsed_value=None, confidence=0.8)
        result = _validate_parsed_value(resp, q)
        assert result.parsed_value is None
        assert result.confidence == 0.8

    def test_int_coercion_success(self):
        from src.agents.interview_executor import _validate_parsed_value

        q = _make_question(expected_type="int")
        resp = TurnResponse(parsed_value="42", confidence=0.9)
        result = _validate_parsed_value(resp, q)
        assert result.parsed_value == 42
        assert result.confidence == 0.9

    def test_int_coercion_failure(self):
        from src.agents.interview_executor import _validate_parsed_value

        q = _make_question(expected_type="int")
        resp = TurnResponse(parsed_value="not_a_number", confidence=0.9)
        result = _validate_parsed_value(resp, q)
        assert result.confidence == 0.0

    def test_float_coercion_success(self):
        from src.agents.interview_executor import _validate_parsed_value

        q = _make_question(expected_type="float")
        resp = TurnResponse(parsed_value="3.14", confidence=0.9)
        result = _validate_parsed_value(resp, q)
        assert result.parsed_value == 3.14

    def test_float_coercion_failure(self):
        from src.agents.interview_executor import _validate_parsed_value

        q = _make_question(expected_type="float")
        resp = TurnResponse(parsed_value="abc", confidence=0.8)
        result = _validate_parsed_value(resp, q)
        assert result.confidence == 0.0

    def test_enum_match_case_insensitive(self):
        from src.agents.interview_executor import _validate_parsed_value

        q = _make_question(expected_type="enum", valid_values=["hub", "spoke"])
        resp = TurnResponse(parsed_value="HUB", confidence=0.9)
        result = _validate_parsed_value(resp, q)
        assert result.parsed_value == "hub"
        assert result.confidence == 0.9

    def test_enum_no_match(self):
        from src.agents.interview_executor import _validate_parsed_value

        q = _make_question(expected_type="enum", valid_values=["hub", "spoke"])
        resp = TurnResponse(parsed_value="transit", confidence=0.9)
        result = _validate_parsed_value(resp, q)
        assert result.confidence == 0.0

    def test_enum_no_valid_values(self):
        from src.agents.interview_executor import _validate_parsed_value

        q = _make_question(expected_type="enum", valid_values=None)
        resp = TurnResponse(parsed_value="anything", confidence=0.9)
        result = _validate_parsed_value(resp, q)
        # No valid_values means no enum check — passes through
        assert result.confidence == 0.9

    def test_list_str_from_csv(self):
        from src.agents.interview_executor import _validate_parsed_value

        q = _make_question(expected_type="list_str")
        resp = TurnResponse(parsed_value="pci, hipaa, soc2", confidence=0.9)
        result = _validate_parsed_value(resp, q)
        assert result.parsed_value == ["pci", "hipaa", "soc2"]

    def test_list_str_already_list(self):
        from src.agents.interview_executor import _validate_parsed_value

        q = _make_question(expected_type="list_str")
        resp = TurnResponse(parsed_value=["pci", "soc2"], confidence=0.9)
        result = _validate_parsed_value(resp, q)
        assert result.parsed_value == ["pci", "soc2"]

    def test_list_str_invalid_type(self):
        from src.agents.interview_executor import _validate_parsed_value

        q = _make_question(expected_type="list_str")
        resp = TurnResponse(parsed_value=42, confidence=0.9)
        result = _validate_parsed_value(resp, q)
        assert result.confidence == 0.0

    def test_str_type_passthrough(self):
        from src.agents.interview_executor import _validate_parsed_value

        q = _make_question(expected_type="str")
        resp = TurnResponse(parsed_value="anything", confidence=0.9)
        result = _validate_parsed_value(resp, q)
        assert result.parsed_value == "anything"
        assert result.confidence == 0.9


# ===================================================================
# Prompt building helpers
# ===================================================================


class TestPromptBuilding:
    def test_build_valid_values_block_with_values(self):
        from src.agents.interview_executor import _build_valid_values_block

        q = _make_question(valid_values=["hub", "spoke"])
        result = _build_valid_values_block(q)
        assert "hub" in result
        assert "spoke" in result

    def test_build_valid_values_block_empty(self):
        from src.agents.interview_executor import _build_valid_values_block

        q = _make_question(valid_values=None)
        assert _build_valid_values_block(q) == ""

    def test_build_next_question_block_with_next(self):
        from src.agents.interview_executor import _build_next_question_block

        next_q = _make_question(field_path="bandwidth", question_template="Bandwidth?")
        result = _build_next_question_block(next_q)
        assert "bandwidth" in result
        assert "Bandwidth?" in result

    def test_build_next_question_block_last_question(self):
        from src.agents.interview_executor import _build_next_question_block

        result = _build_next_question_block(None)
        assert "LAST" in result


# ===================================================================
# execute_turn — integration with mocked agent
# ===================================================================


class TestExecuteTurn:
    def _make_plan_with_pending(self) -> QuestionPlan:
        return QuestionPlan(
            entries=[
                PlannedQuestion(
                    field_path="role",
                    question_template="What role?",
                    expected_type="enum",
                    valid_values=["hub", "spoke"],
                ),
                PlannedQuestion(
                    field_path="branches",
                    question_template="How many branches?",
                    expected_type="int",
                ),
            ],
        )

    def test_returns_complete_when_no_pending(self):
        from src.agents.interview_executor import execute_turn

        plan = QuestionPlan(entries=[
            PlannedQuestion(field_path="done", question_template="?", status="answered"),
        ])

        with patch("src.config.metrics.metrics", MagicMock()):
            result_plan, response = execute_turn(plan, "anything")

        assert response.confidence == 1.0
        assert "complete" in response.response_message.lower()

    @patch("src.agents.interview_executor._create_executor_model")
    @patch("src.agents.interview_executor._invoke_executor")
    def test_normal_answer_advances_plan(self, mock_invoke, mock_model):
        from src.agents.interview_executor import execute_turn

        mock_result = MagicMock()
        mock_result.structured_output = TurnResponse(
            parsed_value="hub",
            confidence=0.95,
            response_message="Great, you want a hub role.",
        )
        mock_invoke.return_value = mock_result

        plan = self._make_plan_with_pending()
        with patch("src.config.metrics.metrics", MagicMock()):
            result_plan, response = execute_turn(plan, "I need a hub")

        assert result_plan.entries[0].status == "answered"
        assert result_plan.entries[0].answered_value == "hub"
        assert response.confidence == 0.95

    @patch("src.agents.interview_executor._create_executor_model")
    @patch("src.agents.interview_executor._invoke_executor")
    def test_low_confidence_does_not_advance(self, mock_invoke, mock_model):
        from src.agents.interview_executor import execute_turn

        mock_result = MagicMock()
        mock_result.structured_output = TurnResponse(
            parsed_value=None,
            confidence=0.2,
            response_message="I'm not sure what you mean.",
        )
        mock_invoke.return_value = mock_result

        plan = self._make_plan_with_pending()
        with patch("src.config.metrics.metrics", MagicMock()):
            result_plan, response = execute_turn(plan, "uhh maybe")

        assert result_plan.entries[0].status == "pending"
        assert "clarify" in response.response_message.lower()

    @patch("src.agents.interview_executor._create_executor_model")
    @patch("src.agents.interview_executor._invoke_executor")
    def test_deviation_detected_does_not_advance(self, mock_invoke, mock_model):
        from src.agents.interview_executor import execute_turn

        mock_result = MagicMock()
        mock_result.structured_output = TurnResponse(
            parsed_value=None,
            confidence=0.3,
            response_message="That's a different requirement.",
            deviation_detected=True,
            deviation_reason="User wants multi-cloud instead of AWS only",
        )
        mock_invoke.return_value = mock_result

        plan = self._make_plan_with_pending()
        with patch("src.config.metrics.metrics", MagicMock()):
            result_plan, response = execute_turn(plan, "I want multi-cloud")

        assert response.deviation_detected is True
        assert result_plan.entries[0].status == "pending"

    @patch("src.agents.interview_executor._create_executor_model")
    @patch("src.agents.interview_executor._invoke_executor")
    def test_deviation_with_good_confidence_marks_answer(self, mock_invoke, mock_model):
        from src.agents.interview_executor import execute_turn

        mock_result = MagicMock()
        mock_result.structured_output = TurnResponse(
            parsed_value="hub",
            confidence=0.8,
            response_message="Hub role noted, but also want multi-region.",
            deviation_detected=True,
            deviation_reason="User wants multi-region",
        )
        mock_invoke.return_value = mock_result

        plan = self._make_plan_with_pending()
        with patch("src.config.metrics.metrics", MagicMock()):
            result_plan, response = execute_turn(plan, "hub but multi-region")

        # Deviation with good confidence still marks the answer
        assert result_plan.entries[0].status == "answered"
        assert response.deviation_detected is True

    @patch("src.agents.interview_executor._create_executor_model")
    @patch("src.agents.interview_executor._invoke_executor")
    def test_fallback_when_no_structured_output(self, mock_invoke, mock_model):
        from src.agents.interview_executor import execute_turn

        mock_result = MagicMock()
        mock_result.structured_output = None  # LLM didn't return structured output
        mock_result.__str__ = lambda self: "Raw text response"

        mock_invoke.return_value = mock_result

        plan = self._make_plan_with_pending()
        with patch("src.config.metrics.metrics", MagicMock()):
            result_plan, response = execute_turn(plan, "hello")

        # Falls back with confidence 0.0, so should request clarification
        assert response.confidence == 0.0
        assert result_plan.entries[0].status == "pending"
