"""Unit tests for the interview service layer.

Tests cover state conversion (_plan_to_progress), helper functions,
and the SSE event generation flow with mocked agents.
"""

from unittest.mock import patch

import pytest

from src.models.interview_plan import PlannedQuestion, QuestionPlan, TurnResponse


# ===================================================================
# _plan_to_progress — pure state conversion
# ===================================================================


class TestPlanToProgress:
    def test_empty_plan_not_complete(self):
        from src.services.interview import _plan_to_progress

        plan = QuestionPlan(
            entries=[
                PlannedQuestion(field_path="availability_requirement", question_template="Availability?", is_blocking=True),
            ],
        )
        progress = _plan_to_progress(plan, "Starting!")
        assert progress.complete is False
        assert progress.response_message == "Starting!"
        # validate_and_correct_completion recalculates missing from InterviewProgress schema
        assert "availability_requirement" in progress.missing_fields

    def test_all_blocking_answered_marks_complete(self):
        from src.services.interview import _plan_to_progress

        # validate_and_correct_completion checks actual InterviewProgress fields.
        # Blocking base fields (minus seeds): availability_requirement, data_sensitivity
        plan = QuestionPlan(
            entries=[
                PlannedQuestion(
                    field_path="availability_requirement",
                    question_template="Availability?",
                    is_blocking=True,
                    status="answered",
                    answered_value="production-multi-az",
                ),
                PlannedQuestion(
                    field_path="data_sensitivity",
                    question_template="Data sensitivity?",
                    is_blocking=True,
                    status="answered",
                    answered_value="confidential",
                ),
            ],
            populated_fields={
                "availability_requirement": "production-multi-az",
                "data_sensitivity": "confidential",
            },
        )
        progress = _plan_to_progress(plan, "Done!")
        assert progress.complete is True
        # Only soft fields remain (user_info, compliance)
        blocking = [f for f in progress.missing_fields
                     if f not in ("user_info.name", "user_info.experience_on_cloud", "compliance")]
        assert blocking == []

    def test_extracts_use_case_fields(self):
        from src.services.interview import _plan_to_progress

        plan = QuestionPlan(
            entries=[
                PlannedQuestion(
                    field_path="realtime-inference.model_size_category",
                    question_template="Model size?",
                    status="answered",
                    answered_value="large",
                    is_blocking=True,
                ),
                PlannedQuestion(
                    field_path="realtime-inference.target_latency_ms",
                    question_template="Target latency?",
                    status="answered",
                    answered_value=50,
                    is_blocking=True,
                ),
            ],
            populated_fields={"realtime-inference": {"model_size_category": "large", "target_latency_ms": 50}},
        )
        progress = _plan_to_progress(plan, "All done")
        assert progress.use_case_fields["model_size_category"] == "large"
        assert progress.use_case_fields["target_latency_ms"] == 50

    def test_includes_auto_filled_use_case_fields(self):
        from src.services.interview import _plan_to_progress

        plan = QuestionPlan(
            entries=[],
            auto_filled={"realtime-inference.model_framework": "tensorrt"},
            populated_fields={},
        )
        progress = _plan_to_progress(plan, "Hi")
        assert progress.use_case_fields.get("model_framework") == "tensorrt"

    def test_populates_base_fields(self):
        from src.services.interview import _plan_to_progress

        plan = QuestionPlan(
            entries=[
                PlannedQuestion(field_path="gpu_budget", question_template="GPU budget?", status="answered",
                                answered_value="high", is_blocking=True),
            ],
            populated_fields={"gpu_budget": "high", "availability_requirement": "production-multi-az"},
        )
        progress = _plan_to_progress(plan, "Good")
        assert progress.gpu_budget == "high"

    def test_invalid_enum_value_reverted(self):
        """LLM-parsed values that don't match catalog choices are reverted."""
        from src.services.interview import _plan_to_progress

        plan = QuestionPlan(
            entries=[
                PlannedQuestion(
                    field_path="gpu_budget",
                    question_template="Budget?",
                    is_blocking=True,
                    status="answered",
                    answered_value="super-duper",  # Not a valid option
                ),
            ],
            populated_fields={"gpu_budget": "super-duper"},
        )
        # Invalid value is reverted — field removed from populated
        progress = _plan_to_progress(plan, "Noted")
        assert progress.complete is False

    def test_invalid_availability_requirement_reverted(self):
        from src.services.interview import _plan_to_progress

        plan = QuestionPlan(
            entries=[
                PlannedQuestion(
                    field_path="availability_requirement",
                    question_template="Availability?",
                    is_blocking=True,
                    status="answered",
                    answered_value="five-nines",  # Not a valid option
                ),
            ],
            populated_fields={"availability_requirement": "five-nines"},
        )
        progress = _plan_to_progress(plan, "Noted")
        # Field was reverted because "five-nines" isn't a valid option
        assert progress.complete is False


# ===================================================================
# _safe_enum
# ===================================================================


class TestSafeEnum:
    def test_valid_value_passes_through(self):
        from src.services.interview import _safe_enum

        assert _safe_enum(None, "ha-single-region-dual-zone") == "ha-single-region-dual-zone"

    def test_any_string_passes_through(self):
        """_safe_enum is a pass-through; field-level validation happens in _plan_to_progress."""
        from src.services.interview import _safe_enum

        assert _safe_enum(None, "multi-az") == "multi-az"

    def test_none_returns_none(self):
        from src.services.interview import _safe_enum

        assert _safe_enum(None, None) is None


# ===================================================================
# _extract_gathered_fields
# ===================================================================


class TestExtractGatheredFields:
    def test_excludes_metadata_keys(self):
        from src.models.requirements import InterviewProgress
        from src.services.interview import _extract_gathered_fields

        progress = InterviewProgress(
            response_message="test",
            gpu_budget="high",
            complete=True,
            missing_fields=[],
            use_case_fields={"model_size_category": "large"},
        )
        gathered = _extract_gathered_fields(progress)
        assert "response_message" not in gathered
        assert "complete" not in gathered
        assert "missing_fields" not in gathered
        assert gathered["gpu_budget"] == "high"
        # use_case_fields are flattened
        assert gathered["model_size_category"] == "large"


# ===================================================================
# _parse_use_cases
# ===================================================================


class TestParseUseCases:
    def test_parses_single(self):
        from src.services.interview import _parse_use_cases

        result = _parse_use_cases("realtime-inference")
        assert len(result) == 1
        assert result[0] == "realtime-inference"

    def test_parses_comma_separated(self):
        from src.services.interview import _parse_use_cases

        result = _parse_use_cases("realtime-inference, training")
        assert len(result) == 2

    def test_parses_all_values(self):
        from src.services.interview import _parse_use_cases

        result = _parse_use_cases("realtime-inference, custom-case")
        assert len(result) == 2
        assert result[1] == "custom-case"

    def test_empty_returns_empty(self):
        from src.services.interview import _parse_use_cases

        assert _parse_use_cases("") == []
        assert _parse_use_cases(None) == []


# ===================================================================
# _interview_chat_events — SSE generation with mocked agents
# ===================================================================


class TestInterviewChatEvents:
    @pytest.mark.asyncio
    @patch("src.services.interview.plan_cache")
    @patch("src.services.interview.generate_plan")
    async def test_turn1_generates_plan(self, mock_gen, mock_cache):
        from src.services.interview import _interview_chat_events

        plan = QuestionPlan(
            entries=[
                PlannedQuestion(field_path="role", question_template="Role?", is_blocking=True),
            ],
        )
        mock_cache.get.return_value = None
        mock_gen.return_value = (plan, "Welcome! What role?")

        events = []
        async for event in _interview_chat_events(
            message="start",
            tenant_id="t1",
            project_id="p1",
            use_case="realtime-inference",
        ):
            events.append(event)

        # Should have a message event and a done event
        assert len(events) == 2
        assert "Welcome" in events[0]
        assert "done" in events[1]
        mock_cache.save.assert_called_once()

    @pytest.mark.asyncio
    @patch("src.services.interview.plan_cache")
    @patch("src.services.interview.generate_plan")
    async def test_input_hint_includes_options_for_enum(self, mock_gen, mock_cache):
        import json
        from src.services.interview import _interview_chat_events

        plan = QuestionPlan(
            entries=[
                PlannedQuestion(
                    field_path="resilience",
                    question_template="Resilience level?",
                    expected_type="enum",
                    valid_values=["none", "ha-single-region-dual-zone"],
                    is_blocking=True,
                ),
            ],
        )
        mock_cache.get.return_value = None
        mock_gen.return_value = (plan, "What resilience?")

        events = []
        async for event in _interview_chat_events(
            message="start", tenant_id="t1", project_id="p1",
        ):
            events.append(event)

        # Parse the SSE message event to check input_hint
        msg_event = events[0]
        # SSE format: "event: message\ndata: {...}\n\n"
        data_line = [line for line in msg_event.split("\n") if line.startswith("data:")][0]
        payload = json.loads(data_line[len("data: "):])
        assert "input_hint" in payload
        assert payload["input_hint"]["field_path"] == "resilience"
        assert payload["input_hint"]["type"] == "enum"
        assert "none" in payload["input_hint"]["options"]

    @pytest.mark.asyncio
    @patch("src.services.interview.plan_cache")
    @patch("src.services.interview.generate_plan")
    async def test_input_hint_omits_options_for_str(self, mock_gen, mock_cache):
        import json
        from src.services.interview import _interview_chat_events

        plan = QuestionPlan(
            entries=[
                PlannedQuestion(
                    field_path="solution_description",
                    question_template="Describe your solution?",
                    expected_type="str",
                    is_blocking=True,
                ),
            ],
        )
        mock_cache.get.return_value = None
        mock_gen.return_value = (plan, "Tell me about your solution")

        events = []
        async for event in _interview_chat_events(
            message="start", tenant_id="t1", project_id="p1",
        ):
            events.append(event)

        data_line = [line for line in events[0].split("\n") if line.startswith("data:")][0]
        payload = json.loads(data_line[len("data: "):])
        assert "input_hint" in payload
        assert payload["input_hint"]["type"] == "str"
        assert "options" not in payload["input_hint"]

    @pytest.mark.asyncio
    @patch("src.services.interview.plan_cache")
    @patch("src.services.interview.execute_turn")
    async def test_turn2_executes_turn(self, mock_exec, mock_cache):
        from src.services.interview import _interview_chat_events

        existing_plan = QuestionPlan(
            entries=[
                PlannedQuestion(field_path="role", question_template="Role?", is_blocking=True),
            ],
        )
        mock_cache.get.return_value = existing_plan
        mock_exec.return_value = (
            existing_plan,
            TurnResponse(
                parsed_value="hub",
                confidence=0.95,
                response_message="Great, hub role!",
            ),
        )

        events = []
        async for event in _interview_chat_events(
            message="hub",
            tenant_id="t1",
            project_id="p1",
        ):
            events.append(event)

        assert len(events) == 2
        assert "hub role" in events[0]
        mock_cache.save.assert_called_once()

    @pytest.mark.asyncio
    @patch("src.services.interview.plan_cache")
    @patch("src.services.interview.execute_turn")
    @patch("src.services.interview.replan")
    async def test_deviation_triggers_replan(self, mock_replan, mock_exec, mock_cache):
        from src.services.interview import _interview_chat_events

        existing_plan = QuestionPlan(
            entries=[
                PlannedQuestion(field_path="role", question_template="Role?", is_blocking=True),
            ],
        )
        mock_cache.get.return_value = existing_plan

        mock_exec.return_value = (
            existing_plan,
            TurnResponse(
                confidence=0.3,
                response_message="That's different.",
                deviation_detected=True,
                deviation_reason="User wants multi-cloud",
            ),
        )

        replanned = QuestionPlan(
            entries=[
                PlannedQuestion(field_path="cloud_provider", question_template="Which clouds?", is_blocking=True),
            ],
        )
        mock_replan.return_value = (replanned, "Updated plan for multi-cloud.")

        events = []
        async for event in _interview_chat_events(
            message="I want multi-cloud",
            tenant_id="t1",
            project_id="p1",
        ):
            events.append(event)

        mock_replan.assert_called_once()
        # Response should contain both the acknowledgment and replan message
        assert "different" in events[0].lower() or "Updated plan" in events[0]

    @pytest.mark.asyncio
    @patch("src.services.interview.plan_cache")
    @patch("src.services.interview.generate_plan")
    async def test_error_yields_sse_error(self, mock_gen, mock_cache):
        from src.services.interview import _interview_chat_events

        mock_cache.get.return_value = None
        mock_gen.side_effect = RuntimeError("LLM failed")

        events = []
        async for event in _interview_chat_events(
            message="start",
            tenant_id="t1",
            project_id="p1",
        ):
            events.append(event)

        assert len(events) == 1
        assert "error" in events[0].lower()
