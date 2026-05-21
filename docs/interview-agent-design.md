# Interview Agent Design

## Overview

The interview agent uses a **plan-then-execute** architecture to gather deployment requirements from users. It splits the work between two models:

- **Planner (Sonnet 4.5)** — generates a structured `QuestionPlan` from seed data + KB context
- **Executor (Haiku 4.5)** — processes one answer per turn (single-shot, no conversation history)

This separation enables fast sub-second turns (Haiku) while preserving planning intelligence (Sonnet) for the initial question sequencing and deviation handling.

## Architecture

```
User message
    │
    ▼
┌─────────────────────────────┐
│ Interview Service            │
│ (src/services/interview.py) │
└──────────┬──────────────────┘
           │
    ┌──────┴──────┐
    │ Turn 1?     │
    ├─────────────┤
    │ YES         │ NO
    ▼             ▼
┌─────────┐  ┌──────────┐
│ Planner │  │ Executor │
│ (Sonnet)│  │ (Haiku)  │
└────┬────┘  └────┬─────┘
     │             │
     ▼             ▼
  QuestionPlan   TurnResponse
     │             │
     └──────┬──────┘
            ▼
     PlanCache (S3-backed)
            │
            ▼
     SSE → Frontend
```

## Question Plan

The `QuestionPlan` is the session state — it replaces LLM conversation history:

```python
class QuestionPlan(BaseModel):
    entries: list[PlannedQuestion]
    populated_fields: dict[str, Any]
    auto_filled: dict[str, Any]
```

Each `PlannedQuestion` contains:
- `field_path` — dotted path (e.g., `gpu_budget` or `realtime-inference.model_size_category`)
- `question_template` — natural language question
- `expected_type` — `enum`, `int`, `float`, `str`, `list_str`
- `valid_values` — allowed options for enum fields
- `skip_conditions` — deterministic pruning rules
- `clarification_attempts` — counter for re-ask limiting

## Catalog-Driven Fields

All interview fields come from `catalog.lock.yaml`:

### Blocking Base Fields
| Field | Type | Options |
|-------|------|---------|
| `gpu_budget` | enum | minimal, moderate, high, unlimited |
| `availability_requirement` | enum | development, staging, production-single-az, production-multi-az |
| `data_sensitivity` | enum | public, internal, confidential, restricted |
| `solution_description` | str | Free-text ML use case description |

### Soft Fields
| Field | Type | Description |
|-------|------|-------------|
| `compliance` | list_str | Compliance frameworks (SOC2, HIPAA, GDPR) |
| `user_info.name` | str | User's name |
| `user_info.experience_on_cloud` | enum | beginner, intermediate, advanced |

### Use-Case-Specific Fields
Each use case (realtime-inference, batch-inference, training) defines additional fields in the catalog that are conditionally asked based on the selected use case.

## Turn Flow

### Turn 1 (Planning)
1. Receive seed data (use_case, initial form selections)
2. Fetch KB context for field enrichment
3. Invoke Sonnet planner → produces `QuestionPlan`
4. Cache plan in S3 (survives server restarts)
5. Stream first question via SSE

### Turns 2+ (Execution)
1. Load plan from cache
2. Get current pending question
3. Invoke Haiku executor with: current question + user message + next question
4. Server-side validation of parsed value
5. If confidence < 0.5: increment `clarification_attempts`, re-ask (max 2 attempts, then present options)
6. If deviation detected: trigger Sonnet re-plan with new context
7. Advance plan, evaluate skip conditions
8. Stream response + updated progress via SSE

### Clarification Limit (`_MAX_CLARIFICATION_ATTEMPTS = 2`)
After 2 failed clarification attempts:
- If `valid_values` exists: present selectable options to the user
- Otherwise: accept the raw value and advance

## Enum Validation

The `_safe_enum()` function validates extracted values against catalog options:
- Case-insensitive matching against `valid_values`
- Returns `None` on mismatch → question is re-asked with options shown

## Deviation Handling (Re-plan)

When the executor detects the user's answer deviates from the planned question (e.g., changing use case mid-interview):
1. Mark current answer if confidence is decent
2. Trigger Sonnet re-plan with updated context
3. New questions are appended to the plan
4. Skip conditions are re-evaluated

## Key Files

| File | Purpose |
|------|---------|
| `src/services/interview.py` | Interview service — plan-to-progress conversion, SSE streaming |
| `src/agents/interview_planner.py` | Sonnet planner — generates QuestionPlan |
| `src/agents/interview_executor.py` | Haiku executor — single-turn answer processing |
| `src/services/plan_cache.py` | Thread-safe plan cache with S3 persistence |
| `src/models/interview_plan.py` | QuestionPlan, PlannedQuestion, TurnResponse models |
| `src/models/requirements.py` | InterviewOutput, InterviewProgress models |

## API Endpoint

```
POST /api/interview/chat
```

Request body:
```json
{
  "message": "I need a real-time inference endpoint for a 70B parameter model",
  "project_id": "my-project",
  "use_case": "realtime-inference",
  "populated_fields": {},
  "requirements": null
}
```

Response: SSE stream with `progress` events containing `InterviewProgress`.

## Models Used

| Role | Model | Setting |
|------|-------|---------|
| Planner | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` | `AI_DEPLOY_PRIMARY_MODEL_ID` |
| Executor | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | `AI_DEPLOY_LIGHTWEIGHT_MODEL_ID` |
