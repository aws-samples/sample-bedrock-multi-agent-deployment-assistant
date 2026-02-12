"""Interview agent — plan-then-execute architecture.

Turn 1 (Planning): Sonnet + KB search → QuestionPlan
Turns 2+ (Execution): Haiku single-shot → parse answer + next question

Public API consumed by src.services.interview:
  - generate_plan(seed_data, use_cases, populated_fields) → (plan, message)
  - execute_turn(plan, user_message) → (plan, turn_response)
  - replan(plan, deviation_reason, use_cases) → (plan, message)
"""

from src.agents.interview_executor import execute_turn
from src.agents.interview_planner import generate_plan, replan

__all__ = ["generate_plan", "execute_turn", "replan"]
