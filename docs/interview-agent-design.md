# Interview Agent v2 — "Plan-then-Execute" Design Document

---

## 1. Problem Statement

The interview agent has to collect requirements from the user about the network configuration they want to deploy.

## 2. Architecture: Plan-then-Execute

### 2.1 Core Idea

Split the interview into two distinct phases:

1. **Planning Phase (Turn 1)**: Sonnet + KB search → generates a `QuestionPlan` (ordered list of questions with skip conditions)
2. **Execution Phase (Turns 2+)**: Haiku processes each answer (parse + humanize next question) — single-shot, no conversation history needed

The **plan is the state**, not the LLM's conversation history.

### 2.2 Phase Diagram

```
┌─────────────────────────────────────────────────────┐
│  TURN 1: PLANNING (Sonnet + KB)                     │
│                                                     │
│  Seed Data ──→ KB Search ──→ Sonnet ──→ Plan        │
│ (use_cases,    (targeted     (structured   (stored  │
│  bandwidth,     query)        output:       server- │
│  description)               QuestionPlan)  side)    │
│                                                     │
│  Output: auto-filled fields + first question        │
│  Latency: ~5-8s (same as current Turn 1)            │
└────────────────────┬────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────┐
│  TURNS 2+: EXECUTION (Haiku, single-shot)           │
│                                                     │
│  User Answer ──→ Haiku ──→ Parse + Respond          │
│  + Plan Entry     (single    (structured output:    │
│  + KB Context      turn,      TurnResponse)         │
│    for field       no                               │
│                    history)                         │
│                                                     │
│  ┌─── Deviation? ──→ YES ──→ KB Re-fetch + Re-plan  │
│  │                            (Sonnet, ~5-8s)       │
│  └── NO ──→ Evaluate skip conditions                │
│             ──→ Pop next question                   │
│             ──→ Return response                     │
│                                                     │
│  Latency: ~1-2s normal, ~5-8s curveball             │
└─────────────────────────────────────────────────────┘
```

### 2.3 Performance Comparison

| Property | Current Agent | v2 Agent |
|---|---|---|
| Turn 1 model | Sonnet | Sonnet |
| Turn 2+ model | Sonnet | **Haiku** |
| Per-turn context | Growing (sliding window) | **Fixed (single-shot)** |
| Question selection | LLM decides each turn | **Server-side plan** |
| Skip logic | None (LLM may or may not skip) | **Deterministic skip conditions** |
| KB search | Turn 1 only | Turn 1 + **curveball re-fetch** |
| Session history | 40-message window | **None** (plan is state) |
| Turn 2+ latency | 3-8s | **1-2s** |
| Total interview | 25-40s | **~12-20s** |

## 3. Data Models

### 3.1 QuestionPlan (Server-Side State)

> **File**: `backend/src/models/interview_plan.py`

```python
SkipOperator = Literal["eq", "neq", "in", "not_in", "exists", "not_exists"]
QuestionStatus = Literal["pending", "answered", "skipped", "auto_filled"]
FieldType = Literal["enum", "int", "float", "str", "list_str"]


class SkipCondition(BaseModel):
    """When to skip a question based on another field's value."""
    field_path: str
    operator: SkipOperator
    value: Any = None  # None valid for exists/not_exists operators


class PlannedQuestion(BaseModel):
    """A single question in the execution plan."""
    field_path: str = Field(description="Dotted path, e.g. 'cloud_routing_protocol' or 'sd-wan.role'")
    question_template: str = Field(description="Natural language question text")
    kb_context: str = Field("", description="Relevant KB snippet for this field")
    expected_type: FieldType = "str"
    valid_values: list[str] | None = None
    is_blocking: bool = True
    is_optional: bool = False
    skip_conditions: list[SkipCondition] = Field(default_factory=list)
    status: QuestionStatus = "pending"
    answered_value: Any = None


class QuestionPlan(BaseModel):
    """Complete interview execution plan — this IS the session state.

    Provides query helpers and mutation methods so the executor never
    needs to manipulate entries directly.
    """
    entries: list[PlannedQuestion] = Field(default_factory=list)
    auto_filled: dict[str, Any] = Field(default_factory=dict)
    auto_fill_rationale: dict[str, str] = Field(default_factory=dict)
    kb_summary: str = ""
    populated_fields: dict[str, Any] = Field(default_factory=dict)  # All gathered values
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    replanned_count: int = 0

    # --- query helpers ---
    def current_question(self) -> PlannedQuestion | None: ...   # First pending entry
    def next_question(self) -> PlannedQuestion | None: ...      # Second pending (for Haiku preview)
    def pending_count(self) -> int: ...
    def blocking_complete(self) -> bool: ...                     # All blocking entries resolved
    def all_missing_field_paths(self) -> list[str]: ...          # Pending field paths

    # --- mutations ---
    def mark_answered(self, field_path: str, value: Any) -> None: ...  # Sets nested via _set_nested()
    def mark_skipped(self, field_path: str) -> None: ...
    def revert_answer(self, field_path: str) -> None: ...              # Used when enum validation fails
    def evaluate_skip_conditions(self) -> list[str]: ...               # Returns newly skipped paths
```

**Utility helpers** (module-level):
- `_get_nested(data, dotted_path)` — resolves `"sd-wan.role"` against a nested dict
- `_set_nested(data, dotted_path, value)` — creates intermediate dicts as needed
- `_evaluate_condition(cond, populated)` — single condition check (returns True → skip)

### 3.2 LLM Structured Outputs

**Planning Phase (Sonnet → QuestionPlanOutput):**

```python
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
    def _coerce_skip_conditions(cls, data):
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
```

> **Design note**: All fields use `default_factory` or sensible defaults. This makes structured output parsing robust — if Sonnet omits a field, we get an empty collection rather than a validation error.

**Execution Phase (Haiku → TurnResponse):**

```python
class TurnResponse(BaseModel):
    """Haiku's structured output for each execution turn."""
    parsed_value: Any = None
    confidence: float = Field(0.0, ge=0.0, le=1.0)  # Constrained range
    response_message: str = ""
    deviation_detected: bool = False
    deviation_reason: str | None = None
```

### 3.3 Return Contract (InterviewProgress)

The service layer converts internal `QuestionPlan` state into `InterviewProgress` via `_plan_to_progress()` before returning to the frontend. The frontend contract is preserved.

> **File**: `backend/src/models/requirements.py`

Key implementation details:
- **Enum validation**: Service layer validates enum fields against `_FIELD_ENUM_REGISTRY` before building `InterviewProgress`. Invalid enum values are reverted (`plan.revert_answer(field_path)`) so the question is re-asked.
- **`validate_and_correct_completion()`**: Server-side enforcement distinguishes blocking fields (`use_cases`, `cloud_routing_protocol`, `resilience`, `bandwidth`, `solution_description` + use-case `required_fields`) from soft fields (`user_info`, `compliance` + use-case `optional_fields`). Soft fields do NOT block `complete=True`.
- **`to_interview_output()`**: Converts `InterviewProgress` → `InterviewOutput`, including `_sanitize_uc_data()` which handles LLM output coercion (e.g., free-text `"latency < 50ms"` → `PerformanceRequirements` model via the model's `_coerce_string` validator).
- **`use_case_fields`**: Flat dict extracted from dotted paths (e.g., `"sd-wan.role"` → `use_case_fields["role"]`).

### 3.4 Enum Registry (Single Source of Truth)

> **File**: `backend/src/agents/interview_planner.py`

```python
_FIELD_ENUM_REGISTRY: dict[str, tuple[type, str]] = {
    "cloud_routing_protocol": (RoutingProtocol, "enum"),
    "resilience": (WorkloadResilience, "enum"),
    "use_cases": (UseCases, "list_str"),
}
```

This registry drives:
1. **`_build_enum_reference()`** — dynamically generates the `{enum_reference}` block injected into all prompts, ensuring LLM always sees the actual Python enum values.
2. **`_ENUM_FIELDS`** in the service layer — derived subset (`etype == "enum"` only) used for server-side validation before building `InterviewProgress`.

### 3.5 USE_CASE_REGISTRY

> **File**: `backend/src/models/requirements.py`

```python
@dataclass
class UseCaseSpec:
    model: type[BaseModel]
    required_fields: set[str]   # blocking — must be populated
    label: str
    optional_fields: set[str] = None  # best-effort (defaults to empty set)
    field_doc_types: dict[str, str] = None  # field → KB document_type mapping

USE_CASE_REGISTRY: dict[UseCases, UseCaseSpec] = {
    UseCases.SD_WAN: UseCaseSpec(
        model=SDWAN,
        required_fields={"role", "number_of_branches"},
        optional_fields={"overlay_strategy", "performance"},
        label="SD-WAN",
        field_doc_types={
            "role": "architecture",
            "number_of_branches": "sizing",
            "overlay_strategy": "configuration",
            "performance": "sizing",
        },
    ),
    UseCases.INSPECTION: UseCaseSpec(
        model=Inspection,
        required_fields={"number_public_ips"},
        optional_fields={"security_features"},
        label="Inspection",
        field_doc_types={
            "number_public_ips": "sizing",
            "security_features": "components",
        },
    ),
}
```

The `field_doc_types` attribute feeds the `get_field_doc_type()` function for targeted Level-2 KB searches.

## 4. Detailed Flow

### 4.1 Turn 1: Plan Generation

```
Frontend: POST /api/interview/chat  (rate-limited: 10/min)
  body: { message: "...", requirements: {bandwidth, description}, use_case: "sd-wan", project_id: "..." }

Backend (src/services/interview.py → src/agents/interview_planner.py):
  1. Session ID = f"interview-{tenant_id}-{project_id}"
  2. plan_cache.get(session_id) → None → enter PLANNING PHASE
  3. Pre-populate seed_data["use_cases"] from use_case param
  4. Level-1 KB search: architecture + components per use case (5 results each)
  5. Build planning prompt: seed context + KB results + missing fields schema + enum reference
  6. bedrock_breaker.pre_check() → fail-fast if circuit is OPEN
  7. asyncio.to_thread(generate_plan, seed_data, use_cases, populated_fields, tenant_id) → Sonnet Agent w/ structured output → QuestionPlanOutput
  7. _enrich_plan(): add is_blocking/is_optional from USE_CASE_REGISTRY, merge seed + auto-filled fields
  8. plan.evaluate_skip_conditions()  ← initial evaluation (some conditions may already be met from seed)
  9. plan_cache.save(session_id, plan)  ← both memory + persistent storage
  10. _plan_to_progress(plan, initial_message) → validate enums → InterviewProgress
  11. Build SSE payload: content, complete, missing_fields, gathered_fields, input_hint
  12. Yield SSE "message" + "done" events (wrapped in with_heartbeats, 15s keep-alive)
```

### 4.2 Turns 2+: Execution

```
Frontend: POST /api/interview/chat
  body: { message: "BGP", populated_fields: {...}, project_id: "..." }

Backend (src/services/interview.py → src/agents/interview_executor.py):
  1. plan_cache.get(session_id) → plan exists → EXECUTION PHASE
  2. bedrock_breaker.pre_check() → fail-fast if circuit is OPEN
  3. asyncio.to_thread(execute_turn, plan, message, tenant_id)
  3. current_question = plan.current_question()  (first pending entry)
  4. next_question = plan.next_question()  (second pending, for Haiku preview)
  5. Build Haiku prompt: current question context + user answer + next question block
  6. Haiku Agent w/ structured output → TurnResponse
  7. _validate_parsed_value(): server-side type coercion + enum validation
  8. IF confidence < 0.5 AND not deviation → re-ask with clarification hint, don't advance plan
  9. IF deviation_detected:
       a. Mark current answer if confidence >= 0.5 (don't lose valid data)
       b. evaluate_skip_conditions()
       c. Return to service layer → asyncio.to_thread(replan, plan, deviation_reason, use_cases, tenant_id)
       d. Level-3 KB re-search (narrowed by deployment_type if known)
       e. Sonnet re-plan → merge: keep answered + auto_filled, replace pending
       f. Combine response messages: acknowledgment + re-plan message
  10. IF normal:
       a. plan.mark_answered(field_path, parsed_value)  ← uses _set_nested for dotted paths
       b. plan.evaluate_skip_conditions() → log newly skipped questions
  11. plan_cache.save(session_id, plan)
  12. _plan_to_progress(plan, response_message) → validate enums → InterviewProgress
  13. Build SSE payload with input_hint for next question:
       { field_path, type, options (if enum) }
  14. If complete:
       a. progress.to_interview_output() → requirements dict
       b. get_store().save_step(tenant_id, project_id, "requirements", requirements)
       c. Include requirements in SSE payload
  15. Yield SSE "message" + "done" events

Error paths:
  - CircuitOpenError → SSE error with retry_after suggestion
  - Any other exception → SSE error "Internal server error"
```

### 4.3 Skip Condition Evaluation

After each answer (and after initial plan generation), `QuestionPlan.evaluate_skip_conditions()` is called. It iterates ALL remaining pending questions:

```python
# QuestionPlan.evaluate_skip_conditions() → list[str]  (newly skipped paths)
def evaluate_skip_conditions(self) -> list[str]:
    skipped: list[str] = []
    for entry in self.entries:
        if entry.status != "pending" or not entry.skip_conditions:
            continue
        if any(_evaluate_condition(c, self.populated_fields) for c in entry.skip_conditions):
            entry.status = "skipped"
            skipped.append(entry.field_path)
    return skipped

# Module-level helper — evaluates a single condition
def _evaluate_condition(cond: SkipCondition, populated: dict) -> bool:
    val = _get_nested(populated, cond.field_path)
    match cond.operator:
        case "eq":         return val == cond.value
        case "neq":        return val != cond.value
        case "in":         return val in (cond.value or [])
        case "not_in":     return val not in (cond.value or [])
        case "exists":     return val is not None
        case "not_exists": return val is None
    return False
```

> **Note**: The `in`/`not_in` operators guard against `None` values with `(cond.value or [])`.

**Example skip conditions (KB-informed):**
- `overlay_strategy`: skip if `sd-wan.role` eq `"spoke"`
- `sd-wan.performance`: skip if `bandwidth` < 100
- All SD-WAN fields: skip if `use_cases` not_in `["sd-wan"]`

**Skip evaluation triggers:**
1. After `generate_plan()` — seed data may already satisfy some conditions
2. After each `execute_turn()` — normal answer flow
3. After `replan()` — new plan may have different skip conditions

### 4.4 Curveball: Deviation + Re-Plan

Haiku flags `deviation_detected=True` when user's answer:
- Mentions a use case not in seed data
- Contradicts an auto-filled assumption
- Introduces requirements outside the current plan scope

**Re-plan flow:**
1. If deviation answer has confidence >= 0.5: mark current answer, evaluate skip conditions
2. KB re-search with updated context (`_search_kb_for_replan()`)
3. Sonnet re-plan: all currently populated fields + new KB results + deviation context
4. Merge: answered + auto_filled entries kept, pending entries replaced with new plan
5. `replanned_count++`, evaluate skip conditions on new plan
6. Combine response: acknowledgment message + re-plan message
7. Continue execution with next pending question

### 4.5 SSE Response Contract

Each turn yields two SSE events: `"message"` (payload) + `"done"` (status: ok). Wrapped in `with_heartbeats()` (15s keep-alive).

**SSE payload structure:**
```json
{
  "content": "response text (acknowledgment + next question)",
  "complete": false,
  "missing_fields": ["cloud_routing_protocol", "sd-wan.role"],
  "gathered_fields": {"bandwidth": 1000, "solution_description": "..."},
  "use_case_fields": {"role": "hub", "number_of_branches": 5},
  "input_hint": {
    "field_path": "cloud_routing_protocol",
    "type": "enum",
    "options": ["bgp", "static-route", "notknown"]
  },
  "requirements": { /* only when complete=true — full InterviewOutput dict */ }
}
```

The `input_hint` enables the frontend to render selectable options for enum fields or typed inputs for numeric fields, without the frontend needing to know the schema.

### 4.6 Configuration

> **File**: `backend/src/config/settings.py` (env prefix: `AI_DEPLOY_`)

| Setting | Default | Usage |
|---|---|---|
| `primary_model_id` | `us.anthropic.claude-sonnet-4-20250514-v1:0` | Planner + replanner |
| `lightweight_model_id` | `us.anthropic.claude-haiku-3-20250310-v1:0` | Executor |
| `interview_max_tokens` | 4096 | Planner/replanner max output tokens |
| `interview_plan_cache_ttl_minutes` | 30 | Plan cache eviction timeout |
| `knowledge_base_id` | None | Bedrock KB ID (None = KB disabled) |
| `storage_backend` | `"local"` | `"local"` or `"aws"` for plan persistence |
| `s3_artifacts_bucket` | `"ai-deploy-artifacts"` | S3 bucket for plan persistence (AWS mode) |
| `s3_knowledge_base_bucket` | `"ai-deploy-knowledge-base"` | KB document source bucket |
| `guardrail_id` / `guardrail_version` | None / `"DRAFT"` | Bedrock guardrails on executor |

## 5. Hierarchical Knowledge Base Search

### 5.1 KB Structure & Metadata

The knowledge base uses a hierarchical S3 structure with metadata attributes:

```
s3://ai-deploy-knowledge-base/
├── {use_case}/
│   └── {deployment_type}/
│       ├── architecture.md          — VPC layout, traffic flows, patterns
│       ├── components.md            — AWS services, product VM sizes, features
│       ├── configuration.md         — Routing, overlay, HA configuration details
│       ├── sizing.md                — Instance sizing, bandwidth, cost
│       └── best-practices.md        — Well-Architected, compliance
```

Each document has a `.metadata.json` sidecar with three filterable attributes:
- `use_case`: `sd-wan | egress | ingress | inspection`
- `deployment_type`: varies per use case (e.g., `hub-spoke`, `dual-hub`, `single-az`)
- `document_type`: `architecture | components | configuration | sizing | best-practices`

See `plans/knowledge-base-setup-guide.md` for full setup instructions.

### 5.2 Search Strategy: Three Levels

The interview agent uses **progressive narrowing** — searches get more specific as context accumulates.

```
PLANNING PHASE (Turn 1):
┌───────────────────────────────────────────────────────────┐
│ Level-1 Search: USE CASE                                  │
│ Filter: use_case={uc} AND document_type IN                │
│         (architecture, components)                        │
│ Query:  "product {uc} deployment architecture on AWS"   │
│ Purpose: Auto-fill standard fields, understand pattern    │
│ Results: 5-8 chunks (architecture overviews + components) │
└───────────────────────────────────────────────────────────┘

EXECUTION PHASE (per-question, only if plan has kb_context gaps):
┌───────────────────────────────────────────────────────────┐
│ Level-2 Search: USE CASE + DOCUMENT TYPE                  │
│ Filter: use_case={uc} AND document_type={relevant_type}   │
│ Query:  "{field_path}-specific query"                     │
│ Purpose: Enrich question context for technical fields     │
│ Results: 2-3 targeted chunks                              │
│ Note: Only for fields where planner's kb_context is thin  │
└───────────────────────────────────────────────────────────┘

CURVEBALL RE-PLAN:
┌───────────────────────────────────────────────────────────┐
│ Level-3 Search: NARROWED BY DEVIATION                     │
│ Filter: use_case={uc} AND deployment_type={dt}            │
│         (deployment_type may have changed from deviation) │
│ Query:  "{deviation_reason} product {uc} {dt}"          │
│ Purpose: Get architecture docs for the corrected pattern  │
│ Results: 5-8 chunks (re-assess auto-fills + questions)    │
└───────────────────────────────────────────────────────────┘
```

### 5.3 Hierarchical KB Search Module

> **File**: `backend/src/tools/kb_search.py`

The module provides two search interfaces:
1. **`kb_search()`** — `@tool`-decorated flat search for the design agent (unchanged contract)
2. **`kb_search_filtered()`** — plain function called directly by the interview planner

```python
def kb_search_filtered(
    query: str,
    *,  # keyword-only after this
    use_case: str | None = None,
    deployment_type: str | None = None,
    document_type: str | list[str] | None = None,
    max_results: int = 5,
) -> list[KBResult]:
    """Hierarchical KB search with Bedrock metadata filtering.

    Builds a filter from the provided metadata attributes and falls back
    to unfiltered vector search if none are provided. Returns an empty
    list when the knowledge base is not configured.
    """
```

**Filter construction** (`_build_kb_filter()`):
- use_case only         → `{"equals": {"key": "use_case", "value": uc}}`
- use_case + doc_type   → `{"andAll": [uc_filter, doc_type_filter]}`
- use_case + deploy     → `{"andAll": [uc_filter, deploy_filter]}`
- all three             → `{"andAll": [uc_filter, deploy_filter, doc_type_filter]}`
- doc_type as list      → `{"in": {"key": "document_type", "value": [...]}}`

**Metadata extraction** (`_extract_metadata_from_uri()`):
```python
# Regex: s3://bucket/use_case/deployment_type/filename.ext
_S3_PATH_RE = re.compile(r"s3://[^/]+/([^/]+)/([^/]+)/([^/]+)\.[^.]+$")
# Returns: {"use_case": group(1), "deployment_type": group(2), "document_type": group(3)}
```

**Result model:**
```python
class KBResult(BaseModel):
    text: str
    source_uri: str
    score: float = 0.0
    use_case: str | None = None       # Extracted from S3 URI path
    deployment_type: str | None = None # Extracted from S3 URI path
    document_type: str | None = None   # Extracted from S3 URI path (filename stem)
```

### 5.4 How Each Phase Uses KB Search

> **File**: `backend/src/agents/interview_planner.py`

**Planning (Turn 1) — `_search_kb_for_planning()`:**
```python
def _search_kb_for_planning(use_cases: list[UseCases], seed_data: dict) -> list[KBResult]:
    results: list[KBResult] = []
    desc = seed_data.get("solution_description", "")
    for uc in use_cases:
        if uc == UseCases.NOTKNOWN:
            continue
        query = f"product {uc.value} deployment architecture AWS {desc}".strip()
        results.extend(
            kb_search_filtered(
                query, use_case=uc.value,
                document_type=["architecture", "components"], max_results=5,
            )
        )
    return results
```

**Execution (per-question enrichment):**
Level-2 enrichment is prepared for but not yet wired in the executor. The `get_field_doc_type()` function in `requirements.py` provides the mapping. Currently, all per-question KB context is set during the planning phase via `PlannedQuestionLLM.kb_context`.

**Curveball (re-plan) — `_search_kb_for_replan()`:**
```python
def _search_kb_for_replan(
    use_cases: list[UseCases], deviation_reason: str, deployment_type: str | None = None,
) -> list[KBResult]:
    results: list[KBResult] = []
    for uc in use_cases:
        if uc == UseCases.NOTKNOWN:
            continue
        query = f"product {uc.value} {deviation_reason}"
        results.extend(
            kb_search_filtered(
                query, use_case=uc.value, deployment_type=deployment_type, max_results=5,
            )
        )
    return results
```

**KB result formatting** (`_format_kb_results()`):
```python
# Each result rendered as: [Source: s3://... | Score: 0.85]\n{text}
# Results separated by "---"
# Empty results → "No knowledge base results available."
```

### 5.5 Field-to-Document-Type Mapping

> **File**: `backend/src/models/requirements.py` — `get_field_doc_type()`

Rather than a hardcoded dict, the mapping is **registry-driven**:

```python
def get_field_doc_type(field_path: str, use_cases: list[UseCases]) -> str:
    """Return the KB document_type for a field path, derived from the registry."""

    # Base fields — hardcoded mapping
    _BASE_DOC_TYPES: dict[str, str | None] = {
        "cloud_routing_protocol": "configuration",
        "resilience": "architecture",
        "compliance": "best-practices",
        "user_info": None,              # No KB needed
        "user_info.name": None,
        "user_info.experience_on_cloud": None,
    }

    # Use-case fields — from USE_CASE_REGISTRY.field_doc_types
    # e.g., "sd-wan.role" → spec.field_doc_types["role"] → "architecture"

    # Fallback: "configuration"
```

**Effective mapping** (derived from registry + base):

| Field Path | Document Type |
|---|---|
| `cloud_routing_protocol` | configuration |
| `resilience` | architecture |
| `compliance` | best-practices |
| `user_info.*` | None (no KB needed) |
| `sd-wan.role` | architecture |
| `sd-wan.number_of_branches` | sizing |
| `sd-wan.overlay_strategy` | configuration |
| `sd-wan.performance` | sizing |
| `inspection.number_public_ips` | sizing |
| `inspection.security_features` | components |
| *(all others)* | configuration (fallback) |

## 6. Plan Storage

> **File**: `backend/src/services/plan_cache.py`

### 6.1 PlanCache Implementation

```python
class PlanCache:
    """Thread-safe plan cache with TTL eviction and persistent storage."""

    _cache: dict[str, tuple[QuestionPlan, float]]  # session_id → (plan, last_access_monotonic)
    _lock: threading.Lock

    def get(session_id: str) -> QuestionPlan | None  # memory → persistent fallback
    def save(session_id: str, plan: QuestionPlan)     # writes to both
    def delete(session_id: str)                        # removes from both

plan_cache = PlanCache()  # module-level singleton
```

**Session ID format**: `f"interview-{tenant_id}-{project_id}"`

**Persistent storage backends**:
- **Local**: `.local-data/{tenant_id}/{project_id}/interview_plan.json`
- **AWS S3**: `s3://{s3_artifacts_bucket}/{tenant_id}/{project_id}/state/interview_plan.json`

**Security**:
- `validate_safe_id()` on tenant_id and project_id — rejects path traversal attempts
- Resolved path checked with `path.is_relative_to(_DATA_DIR.resolve())`

**TTL eviction**: `settings.interview_plan_cache_ttl_minutes` (default: 30). `_evict_stale()` runs on every `get()` call while holding the lock.

### 6.2 Why SessionManager Is Removed

- **No conversation history needed** — each Haiku turn is single-shot with injected context
- **Plan IS the state** — structured Pydantic model, not message arrays
- **Eliminates** `_sanitize_tool_pairs()` entirely
- **No sliding window** — no growing token cost

## 7. Prompt Design

All prompts are stored as `.txt` template files loaded at module import time.

### 7.1 Planning Prompt (Sonnet)

> **File**: `backend/src/prompts/interview_plan.txt` (50 lines)

**Template variables**: `{seed_context_block}`, `{kb_results}`, `{missing_fields_schema}`, `{enum_reference}`

Key instructions (summarized):
1. **Auto-fill** fields that have a clear KB-derived value (with 1-sentence rationale each).
2. **Generate one question per remaining field** with: `field_path`, `question_template`, `kb_context`, `expected_type`, `valid_values`, `skip_conditions` (MUST be JSON array, never bare object).
3. `valid_values` for enums must be **VERBATIM** from `{enum_reference}` — no abbreviation or invention.
4. **Order**: blocking first (by dependency — fields that inform skip conditions come earlier), then optional.
5. `initial_message`: acknowledge goals + list auto-fills + ask Q1 (under 200 words).
6. Do NOT ask about `[SEED_CONTEXT]` fields, do NOT invent fields beyond schema.

### 7.2 Execution Prompt (Haiku)

> **File**: `backend/src/prompts/interview_execute.txt` (34 lines)

**Template variables**: `{field_path}`, `{question_template}`, `{expected_type}`, `{valid_values_block}`, `{kb_context}`, `{user_message}`, `{next_question_block}`

Key instructions (summarized):
1. **Parse** user answer to expected type with confidence 0.0-1.0. Enum `parsed_value` must be an exact valid value string (case-insensitive match). Extract numbers from shorthand ("5 Gbps" → 5000).
2. **Optional fields**: accept "skip"/"none"/"N/A"/"I don't know" → `parsed_value=null, confidence=1.0`.
3. **Respond** in `response_message`: brief acknowledgment (reference KB if relevant) + ask next question (under 100 words).
4. **Deviation**: only for explicit contradictions or new use cases — NOT for minor clarifications.

**Helper functions** in executor:
- `_build_valid_values_block(question)` — renders `"Valid values: bgp, static-route"` or empty string
- `_build_next_question_block(next_q)` — renders next question or "LAST question" summary instruction

### 7.3 Re-Planning Prompt (Sonnet)

> **File**: `backend/src/prompts/interview_replan.txt` (35 lines)

**Template variables**: `{deviation_reason}`, `{populated_fields}`, `{kb_results}`, `{remaining_fields_schema}`, `{enum_reference}`

Key instructions (summarized):
1. Re-evaluate auto-fill opportunities given deviation + new KB results.
2. Generate updated questions for remaining fields (same format as original plan).
3. Update skip conditions to reflect new reality.
4. `initial_message`: explain what changed + ask next question (under 100 words).
5. Do NOT re-ask fields already in "Currently Populated Fields".

## 8. Error Handling

### 8.1 Haiku Parse Failure (confidence < 0.5)

1. Server-side validation (`_validate_parsed_value()`) performs type coercion and enum matching. Sets `confidence=0.0` on failure.
2. If `confidence < 0.5` and no deviation detected: re-ask same question with hint:
   `"Could you clarify? {question_template} For example: {valid_values[0]}."`
3. Plan is NOT advanced — the same question remains `"pending"`.
4. `_MAX_CLARIFICATION_ATTEMPTS = 2` is defined but **not yet enforced** as a hard limit (TODO: escalate to Sonnet after 2 failures on the same field).

### 8.2 Plan Generation Failure

1. **Retry**: `@bedrock_retry("interview-planner")` — 3 attempts, exponential backoff (2-10s), retries on transient AWS/network errors.
2. **Circuit breaker**: Each entry point (`generate_plan()`, `execute_turn()`, `replan()`) calls `bedrock_breaker.pre_check()` at the top to fail-fast if the circuit is OPEN. The actual agent invocation is then wrapped in `bedrock_breaker.call()` which tracks success/failure for state transitions. After 5 consecutive failures → OPEN state → rejects calls for 30s recovery.
3. **Structured output miss**: If Sonnet doesn't return `QuestionPlanOutput`, fall back to `_fallback_plan()`:
   - Builds minimal plan from `get_missing_fields_schema()` — field descriptions as questions
   - No KB context, no skip conditions, `expected_type="str"` for all fields
   - Initial message is empty string

### 8.3 Executor Structured Output Failure

If Haiku doesn't return `TurnResponse`, the raw text output is wrapped:
```python
TurnResponse(response_message=str(result), confidence=0.0)
```
This triggers the low-confidence re-ask flow (§8.1).

### 8.4 Session Recovery

Plan loaded from persistent storage (local JSON or S3) on next request. All answered/auto-filled entries preserved. Execution continues from next pending question.

### 8.5 Empty KB Results

`_format_kb_results()` returns `"No knowledge base results available."` — prompt still works but Sonnet has no auto-fill basis. All fields become questions. Templates derived from Pydantic field descriptions. Skip conditions from schema structure only.

### 8.6 Circuit Breaker

> **File**: `backend/src/config/circuit_breaker.py`

Service layer catches `CircuitOpenError` and returns an SSE error with `retry_after` hint to the frontend. States: CLOSED (normal) → OPEN (rejecting, 30s) → HALF_OPEN (testing).

### 8.7 Rate Limiting

The `/api/interview/chat` endpoint is rate-limited to **10 requests/minute** per client via `@limiter.limit("10/minute")`.

## 9. Observability

### Metrics

> **File**: `backend/src/config/metrics.py` — `MetricsPublisher` (CloudWatch, namespace: `AI Deploy`)

Metrics are recorded via `metrics.record_latency(agent_name, duration_ms, tenant_id)` and `metrics.record_retry(agent_name, attempt_number)`:

| Metric | Dimension | When |
|---|---|---|
| `BedrockInvocationLatencyMs` | `AgentName=interview-planner` | Turn 1 plan generation |
| `BedrockInvocationLatencyMs` | `AgentName=interview-executor` | Each execution turn |
| `BedrockInvocationLatencyMs` | `AgentName=interview-replanner` | Curveball re-plans |
| `RetryAttempt` | `AgentName=interview-*` | Each tenacity retry |
| `RateLimitExceeded` | `AgentName=*` | Bedrock throttling events |

Published asynchronously via `ThreadPoolExecutor` (max 2 workers), non-blocking. All latency metrics include `tenant_id` as an additional `TenantId` dimension for per-tenant observability.

### Logging

> All agents use `LoggingCallbackHandler` from `backend/src/config/callback.py` for tool-level tracing.

| Event | Level | Content |
|---|---|---|
| Plan generation | INFO | Auto-filled count, question count, pending count |
| Each turn | INFO | `field_path`, `parsed_value`, `deviation_detected` |
| Low confidence | INFO | Confidence score, field path, "requesting clarification" |
| Skip conditions | INFO | Which question was skipped (field path) |
| Re-planning | INFO | Remaining question count, replan number |
| Deviation | INFO | Field path, deviation reason |
| Fallback plan | WARNING | "Planner did not return structured output" |
| Executor fallback | WARNING | "Executor did not return structured output" |
| Invalid enum | WARNING | Enum class name, rejected value |
| Cache eviction | INFO | Count of stale entries evicted |
