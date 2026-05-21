# Documentation Agent Design

## Overview

The documentation agent generates **2 deliverables** after IaC generation completes:

1. **Architecture Diagram** — Mermaid `architecture-beta` diagram with AWS icons
2. **User Guide** — comprehensive deployment/readme guide in Markdown

Both are generated in parallel via `asyncio.gather()`. The diagram goes through a validate-fix loop using Node.js `mermaid.parse()` for real syntax validation.

## Architecture

```
┌────────────────────────────────────────┐
│ generate_documentation()               │
│ (src/agents/documentation.py)          │
├────────────────────────────────────────┤
│                                        │
│  asyncio.gather(                       │
│    _diagram_task(),  ←── Sonnet + validate-fix loop
│    _guide_task(),    ←── Sonnet single call
│  )                                     │
│                                        │
│  on_section_complete callback ──→ WebSocket notification
│                                        │
└────────────────────────────────────────┘
```

## Deliverables

### Architecture Diagram

- **Model:** Sonnet 4.5 (`AI_DEPLOY_PRIMARY_MODEL_ID`)
- **Token limit:** `docs_diagram_max_tokens` (default: 16384)
- **Validation:** Node.js `mermaid.parse()` via subprocess
- **Fix loop:** Up to `docs_diagram_max_fix_attempts` (default: 3) iterations
- **Fix token limit:** `docs_diagram_fix_max_tokens` (default: 16384)
- **Output:** Mermaid `architecture-beta` syntax with `@aws-icons`

Flow:
1. Generate diagram from design + requirements + CFN template
2. Validate via `validate_mermaid` tool (Node.js subprocess)
3. If invalid: feed errors back to Sonnet for fix (up to N attempts)
4. Track `diagram_fix_attempts` and `diagram_validation_passed` in output

### User Guide

- **Model:** Sonnet 4.5 (`AI_DEPLOY_PRIMARY_MODEL_ID`)
- **Token limit:** `docs_user_guide_max_tokens` (default: 32768)
- **Output:** Markdown deployment guide (~2500 words with tables)

Content includes: architecture overview, deployment steps, configuration reference, security considerations, and operational guidance.

## Data Model

```python
class DocumentationOutput(BaseModel):
    user_guide: str
    architecture_diagram: str
    diagram_fix_attempts: int
    diagram_validation_passed: bool

VALID_DOC_SECTIONS: set[str] = {"user_guide", "architecture_diagram"}
```

## Async Processing

Documentation generation runs as an async task:

1. `POST /api/docs/submit` → enqueues to SQS FIFO (or local worker)
2. Worker calls `process_docs_task()` in `docs_processing.py`
3. Worker invokes `generate_documentation()` with project state
4. Progress notifications via WebSocket (per-section callbacks)
5. Result stored via `store.save_step()` + `persist_artifacts()` to S3

## Section Regeneration

Individual sections can be regenerated without re-running the full pipeline:

```
POST /api/docs/regenerate-section
{"project_id": "...", "section": "architecture_diagram"}
```

This calls `regenerate_section()` which re-runs only the specified section's generator and updates the stored docs.

## Key Files

| File | Purpose |
|------|---------|
| `src/agents/documentation.py` | Agent implementation — diagram + guide generation |
| `src/services/docs_processing.py` | Async task processing pipeline |
| `src/services/docs.py` | Task submission service |
| `src/models/docs.py` | DocumentationOutput, DocsTask models |
| `src/tools/mermaid_validator.py` | Node.js mermaid.parse() subprocess tool |
| `src/prompts/docs_diagram.txt` | Diagram generation prompt template |
| `src/prompts/docs_diagram_fix.txt` | Diagram fix prompt template |
| `src/prompts/docs_user_guide.txt` | User guide prompt template |

## Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `AI_DEPLOY_DOCS_DIAGRAM_MAX_TOKENS` | 16384 | Diagram generation token limit |
| `AI_DEPLOY_DOCS_DIAGRAM_FIX_MAX_TOKENS` | 16384 | Diagram fix token limit |
| `AI_DEPLOY_DOCS_USER_GUIDE_MAX_TOKENS` | 32768 | User guide token limit |
| `AI_DEPLOY_DOCS_DIAGRAM_MAX_FIX_ATTEMPTS` | 3 | Max validation-fix iterations |
