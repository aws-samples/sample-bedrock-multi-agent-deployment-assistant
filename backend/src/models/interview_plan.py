"""Interview plan models — server-side state for the plan-then-execute interview.

The QuestionPlan replaces LLM conversation history as the session state.
Each turn, the executor pops the next pending question, parses the user's
answer, evaluates skip conditions, and advances the plan.
"""

import logging
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Skip conditions — deterministic question pruning
# ---------------------------------------------------------------------------

SkipOperator = Literal["eq", "neq", "in", "not_in", "exists", "not_exists"]


class SkipCondition(BaseModel):
    """When to skip a question based on another field's value."""

    field_path: str
    operator: SkipOperator
    value: Any = None


# ---------------------------------------------------------------------------
# Planned questions
# ---------------------------------------------------------------------------

QuestionStatus = Literal["pending", "answered", "skipped", "auto_filled"]
FieldType = Literal["enum", "int", "float", "str", "list_str"]


class PlannedQuestion(BaseModel):
    """A single question in the execution plan."""

    field_path: str = Field(description="Dotted path, e.g. 'gpu_budget' or 'realtime-inference.model_size_category'")
    question_template: str = Field(description="Natural language question text")
    kb_context: str = Field("", description="Relevant KB snippet for this field")
    expected_type: FieldType = "str"
    valid_values: list[str] | None = None
    is_blocking: bool = True
    is_optional: bool = False
    skip_conditions: list[SkipCondition] = Field(default_factory=list)
    status: QuestionStatus = "pending"
    answered_value: Any = None


# ---------------------------------------------------------------------------
# Question plan — the interview session state
# ---------------------------------------------------------------------------


def _get_nested(data: dict, dotted_path: str) -> Any:
    """Resolve a dotted field path against a nested dict."""
    parts = dotted_path.split(".")
    current: Any = data
    for part in parts:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _evaluate_condition(cond: SkipCondition, populated: dict) -> bool:
    """Return True if the condition is satisfied (question should be skipped)."""
    val = _get_nested(populated, cond.field_path)
    match cond.operator:
        case "eq":
            return val == cond.value
        case "neq":
            return val != cond.value
        case "in":
            return val in (cond.value or [])
        case "not_in":
            return val not in (cond.value or [])
        case "exists":
            return val is not None
        case "not_exists":
            return val is None
    return False


class QuestionPlan(BaseModel):
    """Complete interview execution plan — this IS the session state."""

    entries: list[PlannedQuestion] = Field(default_factory=list)
    auto_filled: dict[str, Any] = Field(default_factory=dict)
    auto_fill_rationale: dict[str, str] = Field(default_factory=dict)
    kb_summary: str = ""
    populated_fields: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    replanned_count: int = 0

    # --- query helpers ---

    def current_question(self) -> PlannedQuestion | None:
        """First entry with status='pending'."""
        return next((e for e in self.entries if e.status == "pending"), None)

    def next_question(self) -> PlannedQuestion | None:
        """Second pending entry (preview for Haiku prompt)."""
        pending = [e for e in self.entries if e.status == "pending"]
        return pending[1] if len(pending) > 1 else None

    def pending_count(self) -> int:
        return sum(1 for e in self.entries if e.status == "pending")

    def blocking_complete(self) -> bool:
        """True when every blocking entry is answered, skipped, or auto-filled."""
        return all(
            e.status != "pending"
            for e in self.entries
            if e.is_blocking  # nosemgrep: is-function-without-parentheses - is_blocking is a Pydantic bool field
        )

    def all_missing_field_paths(self) -> list[str]:
        """Field paths for all pending entries (blocking + optional)."""
        return [e.field_path for e in self.entries if e.status == "pending"]

    # --- mutations ---

    def mark_answered(self, field_path: str, value: Any) -> None:
        for entry in self.entries:
            if entry.field_path == field_path and entry.status == "pending":
                entry.status = "answered"
                entry.answered_value = value
                # Store in populated_fields with dotted-path nesting
                _set_nested(self.populated_fields, field_path, value)
                return

    def mark_skipped(self, field_path: str) -> None:
        for entry in self.entries:
            if entry.field_path == field_path and entry.status == "pending":
                entry.status = "skipped"
                return

    def revert_answer(self, field_path: str) -> None:
        """Revert an answered entry back to pending (e.g., invalid enum value)."""
        for entry in self.entries:
            if entry.field_path == field_path and entry.status == "answered":
                entry.status = "pending"
                entry.answered_value = None
                self.populated_fields.pop(field_path, None)
                return

    def evaluate_skip_conditions(self) -> list[str]:
        """Evaluate all pending entries and skip those whose conditions are met.

        Returns field_paths of entries that were just skipped.
        """
        skipped: list[str] = []
        for entry in self.entries:
            if entry.status != "pending" or not entry.skip_conditions:
                continue
            if any(_evaluate_condition(c, self.populated_fields) for c in entry.skip_conditions):
                entry.status = "skipped"
                skipped.append(entry.field_path)
                logger.info("Skipped question for '%s' (condition met)", entry.field_path)
        return skipped


def _set_nested(data: dict, dotted_path: str, value: Any) -> None:
    """Set a value in a nested dict using a dotted path."""
    parts = dotted_path.split(".")
    for part in parts[:-1]:
        data = data.setdefault(part, {})
    data[parts[-1]] = value


# ---------------------------------------------------------------------------
# LLM structured outputs
# ---------------------------------------------------------------------------


class PlannedQuestionLLM(BaseModel):
    """Subset generated by Sonnet during plan creation."""

    field_path: str
    question_template: str
    kb_context: str = ""
    expected_type: FieldType = "str"
    valid_values: list[str] | None = None
    skip_conditions: list[SkipCondition] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _coerce_skip_conditions(cls, data: Any) -> Any:
        """LLMs often return a single dict instead of a list for skip_conditions."""
        if isinstance(data, dict) and "skip_conditions" in data:
            sc = data["skip_conditions"]
            if isinstance(sc, dict):
                data["skip_conditions"] = [sc]
        return data


class QuestionPlanOutput(BaseModel):
    """Sonnet's structured output during plan generation."""

    auto_filled_fields: dict[str, Any] = Field(default_factory=dict)
    auto_fill_rationale: dict[str, str] = Field(default_factory=dict)
    questions: list[PlannedQuestionLLM] = Field(default_factory=list)
    kb_summary: str = ""
    initial_message: str = Field(description="First response: acknowledge seed, list auto-fills, ask Q1")


class TurnResponse(BaseModel):
    """Haiku's structured output for each execution turn."""

    parsed_value: Any = None
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    response_message: str = ""
    deviation_detected: bool = False
    deviation_reason: str | None = None
