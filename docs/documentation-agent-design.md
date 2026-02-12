# Documentation Agent — Architecture Design

## Overview

The documentation agent generates 3 deliverables after IaC generation completes:

1. **Architecture Diagram** — Mermaid `architecture-beta` with AWS icon packs, left-to-right layout
2. **User Guide** — deployment guide (~3000 words, Markdown)
3. **STRIDE Threat Model** — security analysis (~3000 words, Markdown)

All 3 sections run in parallel via `asyncio.gather()`. The diagram additionally goes through a **validate-fix loop** using Node.js `mermaid.parse()` — the same parser the frontend uses — guaranteeing that any passing diagram renders correctly in the browser.

---

## Architecture

```
┌──────────────────────────────────────────────────┐
│            asyncio.gather() — 3 parallel         │
├──────────────┬───────────────┬───────────────────┤
│  Diagram     │  User Guide   │  Threat Model     │
│  generate()  │  generate()   │  generate()       │
│  ↓           │  ↓ notify     │  ↓ notify         │
│  validate()  │  (or error)   │  (or error)       │
│  ↓ (fail?)   │               │                   │
│  fix() ──┐   │               │                   │
│  ↓       │   │               │                   │
│  validate│ x3│               │                   │
│  ↓ notify│   │               │                   │
├──────────┴───┴───────────────┴───────────────────┤
│              DocumentationOutput                 │
└──────────────────────────────────────────────────┘
```

### Key Design Decisions

- **3 parallel calls** — each section is a single LLM call producing complete output. No chunk assembly, no section merging.
- **~3000 word target** for text sections — keeps generation fast (~30-60s per section) while providing thorough documentation with tables.
- **`asyncio.to_thread()`** for blocking LLM calls — lets `@bedrock_retry` (tenacity) work correctly inside each thread.
- **Node.js `mermaid.parse()`** for diagram validation — replaces lossy regex sanitization. The validator runs as a subprocess.
- **External `.txt` prompt files** in `backend/src/prompts/` — matches the IaC agent pattern, enabling prompt iteration without code changes.
- **Progressive rendering with error notifications** — each section calls `on_section_complete(section_name, content)` on both success AND failure. The frontend always gets progress updates.
- **Best-effort on failure** — if the diagram fails all validation attempts, the last version is returned anyway. If any section throws, the others still complete (via `return_exceptions=True`), and error placeholder text is sent to the frontend.
- **Dedicated SQS queue** — docs tasks use their own FIFO queue with a dedicated Lambda worker, not shared with design.

---

## Data Models

### DocumentationOutput

```python
# backend/src/models/docs.py

class DocumentationOutput(BaseModel):
    user_guide: str = Field(default="", description="Complete deployment guide (Markdown)")
    threat_model: str = Field(default="", description="STRIDE threat model (Markdown)")
    architecture_diagram: str = Field(default="", description="Mermaid architecture-beta code")
    diagram_fix_attempts: int = Field(default=0, description="Number of diagram fix attempts")
    diagram_validation_passed: bool = Field(default=False, description="Whether diagram passed validation")
```

### DocsTask

```python
class DocsTaskStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

class DocsTask(BaseModel):
    task_id: str
    tenant_id: str
    project_id: str
    task_type: str = "docs"
    status: DocsTaskStatus = DocsTaskStatus.QUEUED
    submitted_at: str
    started_at: str | None = None
    completed_at: str | None = None
    result: dict | None = None       # DocumentationOutput.model_dump()
    error_message: str | None = None
    ttl: int | None = None            # 7-day DynamoDB TTL
```

### Frontend Types

```typescript
export interface DocumentationOutput {
  user_guide: string;
  threat_model: string;
  architecture_diagram: string;
  diagram_fix_attempts: number;
  diagram_validation_passed: boolean;
}

export type DocsTaskStatus = 'queued' | 'processing' | 'completed' | 'failed';

export interface DocsTaskResponse {
  task_id: string;
  status: DocsTaskStatus;
  submitted_at?: string;
  result?: DocumentationOutput;
  error?: string;
}
```

---

## Agent Names & Tool Policies

Four named agents, all with empty tool sets (LLM-only, no function calling):

| Agent Name | Function | Retry Decorator |
|------------|----------|----------------|
| `docs-diagram` | Initial diagram generation | `@bedrock_retry("docs-diagram")` |
| `docs-diagram-fix` | Fix diagram from validation errors | `@bedrock_retry("docs-diagram-fix")` |
| `docs-user-guide` | Complete user guide | `@bedrock_retry("docs-user-guide")` |
| `docs-threat-model` | Complete STRIDE threat model | `@bedrock_retry("docs-threat-model")` |

---

## Settings

```python
# Documentation agent token limits
docs_diagram_max_tokens: int = 16384       # Architecture diagram generation
docs_diagram_fix_max_tokens: int = 16384   # Diagram fix attempts
docs_user_guide_max_tokens: int = 32768    # User guide (~3000 words with tables)
docs_threat_model_max_tokens: int = 32768  # STRIDE threat model (~3000 words with tables)
docs_diagram_max_fix_attempts: int = 3     # Max diagram validation-fix iterations

# Documentation async processing
sqs_docs_queue_url: Optional[str] = None   # If None, use local worker
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_LCM_DOCS_DIAGRAM_MAX_TOKENS` | 16384 | Token limit for diagram generation |
| `AI_LCM_DOCS_DIAGRAM_FIX_MAX_TOKENS` | 16384 | Token limit for diagram fix attempts |
| `AI_LCM_DOCS_USER_GUIDE_MAX_TOKENS` | 32768 | Token limit for user guide (~3000 words with tables) |
| `AI_LCM_DOCS_THREAT_MODEL_MAX_TOKENS` | 32768 | Token limit for STRIDE threat model (~3000 words with tables) |
| `AI_LCM_DOCS_DIAGRAM_MAX_FIX_ATTEMPTS` | 3 | Max diagram validation-fix iterations |
| `AI_LCM_SQS_DOCS_QUEUE_URL` | None | Dedicated docs SQS FIFO queue URL |

---

## System Prompts

Two inline system prompts (short role definitions, not in `.txt` files):

- **`_DIAGRAM_SYSTEM_PROMPT`** — "You are an expert AWS architecture diagram generator..."
- **`_TEXT_SYSTEM_PROMPT`** — "You are an FCCS Technical Writer producing documentation for a FortiGate-VM deployment on AWS..."

User prompts with full context are loaded from the `.txt` template files.

---

## Data Flow

```
docs.py (API)
  -> SQS docs queue (AWS) or local_worker (dev)
    -> docs_worker.py (Lambda) or local_worker.py (dev)
      -> docs_processing.py
        -> asyncio.new_event_loop().run_until_complete(
            generate_documentation(
              design, requirements_json, cft_template,
              on_section_complete=callback,
              tenant_id=tenant_id, project_id=project_id
            )
          )
        -> bedrock_breaker.pre_check() called at entry
        -> generate_documentation() runs 3 tasks via asyncio.gather():
            |-- _diagram_task() -> _generate_diagram() -> validate -> fix loop -> notify
            |-- _guide_task()   -> _generate_user_guide() -> notify
            +-- _threat_task()  -> _generate_threat_model() -> notify
            (each task notifies on BOTH success and failure)
        -> DocumentationOutput saved to store
        -> WebSocket notification ("completed")
```

### Progressive Rendering

Each section calls `on_section_complete(section_name, content)` when it finishes — including on failure (with error placeholder text). The processing pipeline relays this to `section_notify_fn`, which sends a WebSocket `docs_section` message. The frontend `DocsLoading` component derives step completion from the actual `docs` object — no fake timers.

---

## Diagram Layout Strategy

The diagram prompt enforces a strict **left-to-right** network flow:

```
Internet -> [Public Subnets] -> FortiGate -> [Private Subnets] -> Data Layer -> IAM/Monitoring
```

Rules:
- Maximum 15-20 service nodes (combine similar resources)
- 8-12 edges total (primary data flow only, no every-reference edges)
- Short single-hop edges: `source:R -- L:target`
- Edge priority: ingress path -> inspected traffic -> app flow -> control plane

---

## Diagram Validation-Fix Loop

```
_generate_diagram(cft_template)
  |
for attempt in 1..max_fix_attempts:
  validate_mermaid(diagram)  ->  subprocess: node tools/validate-mermaid/index.mjs
  |-- valid?   -> return (diagram, attempt, True)
  +-- invalid? -> _fix_diagram(diagram, errors, cft_template)
                   |
                   (loop continues)
  |
return (diagram, max_attempts, False)  # best effort
```

The `validate_mermaid()` Python wrapper gracefully degrades:
- **Node.js not installed** -> returns `(True, "")` (skip validation)
- **Validator script missing** -> returns `(True, "")` (skip validation)
- **Subprocess timeout (15s)** -> returns `(False, "Validation timed out")`

---

## Error Handling

- `asyncio.gather(return_exceptions=True)` — if one section throws, the others still complete.
- Each task wrapper catches exceptions and sends error notification BEFORE re-raising.
- Each result is checked with `isinstance(results[i], BaseException)` with full `exc_info` logging.
- Failed sections get placeholder text (e.g., `"*User guide generation failed*"`). Error details are logged but NOT leaked to the client in the placeholder text.
- Diagram fix loop returns best-effort output even if validation never passes.
- `bedrock_breaker.pre_check()` called before generation starts (circuit breaker pattern).

---

## Retry Strategy

### Three Layers

| Layer | Scope | Mechanism | Attempts | Backoff |
|-------|-------|-----------|----------|---------|
| Section | Single LLM call | `@bedrock_retry` (tenacity) | 3 | Exponential 2-10s |
| Circuit Breaker | All Bedrock calls | `bedrock_breaker` | N/A | 30s recovery |
| Task | Entire docs task | SQS DLQ | 3 | Immediate re-delivery |

Individual section failures produce placeholder text but do not fail the entire task. If all sections fail, the task is marked FAILED and SQS retries.

---

## Infrastructure (AWS)

| Resource | Config |
|----------|--------|
| **SQS Queue** | `ai-lcm-docs-tasks.fifo` — dedicated FIFO, 10-min visibility, KMS encrypted |
| **SQS DLQ** | `ai-lcm-docs-dlq.fifo` — 14-day retention, maxReceiveCount=3 |
| **Lambda** | `ai-lcm-docs-worker` — Docker image, 2GB RAM, 10-min timeout |
| **Lambda CMD** | `src.workers.docs_worker.handler` (overrides Dockerfile default) |
| **ECS env var** | `AI_LCM_SQS_DOCS_QUEUE_URL` -> maps to `sqs_docs_queue_url` setting |
| **Node.js** | Installed in both `Dockerfile` and `Dockerfile.lambda` for Mermaid validation |

### Local Dev

- Requires Node.js (already present for frontend). Run `npm install` in `backend/tools/validate-mermaid/`.
- `sqs_docs_queue_url` is `None` -> tasks go to local background worker thread.
- WebSocket section notifications work locally via `ws_manager.notify()` -> `run_coroutine_threadsafe`.

### `backend/lambdas/` vs `backend/src/workers/`

These are **separate deployment patterns** for different Lambda types:
- `backend/lambdas/ws/` — Lightweight **code asset** Lambdas for WebSocket API Gateway (Python 3.12 runtime, no Docker)
- `backend/src/workers/` — Heavy **Docker image** Lambdas for async processing (Bedrock, Node.js, full app). Also serve as the local worker thread in dev mode.

### Lambda Handler

```python
# backend/src/workers/docs_worker.py
def handler(event, context):
    for record in event.get("Records", []):
        body = json.loads(record["body"])
        task_id = body.get("task_id", "?")
        logger.info("Received SQS message for docs task %s", task_id)
        try:
            process_docs_task(body, notify_fn=None)
        except Exception:
            logger.exception("Failed to process docs task %s", task_id)
            try:
                mark_docs_task_failed(body, notify_fn=None)
            except Exception:
                logger.exception(
                    "Failed to mark docs task %s as FAILED — allowing SQS retry",
                    task_id,
                )
                raise  # Re-raise so SQS retries (FAILED status not persisted)
```

### Service Routing

```python
# docs.py
if settings.sqs_docs_queue_url:
    _submit_to_sqs(body)  # Includes MessageGroupId: f"{tenant_id}#{project_id}"
else:
    _enqueue_local(body)
```

---

## API Contract

### Submit Documentation Task

```
POST /api/docs/submit?tenant_id={tenant_id}
{ "project_id": "abc123" }

-> 200: { "task_id": "docs-abc123-...", "status": "queued" }
```

### Poll Task Status

```
GET /api/docs/task/{task_id}?tenant_id={tenant_id}

-> 200: { "task_id": "...", "status": "completed", "result": { ... } }
```

### WebSocket Notifications

```json
{"type": "docs_section", "section": "architecture_diagram", "content": "..."}
{"type": "docs_section", "section": "user_guide", "content": "..."}
{"type": "docs_section", "section": "threat_model", "content": "..."}
{"type": "docs_complete", "task_id": "...", "result": {...}}
{"type": "docs_failed", "task_id": "...", "error": "..."}
```

---
