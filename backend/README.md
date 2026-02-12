# Backend

Python 3.12 / FastAPI / Strands Agents / AWS Bedrock

AI-powered backend that orchestrates a 4-stage agent pipeline for FortiGate deployment lifecycle management.

## Quick Start

```bash
uv sync                                                            # install deps
cp .env.sample .env                                                # configure environment
uv run uvicorn src.main:app --host 127.0.0.1 --port 8000 --reload  # dev server
```

Health check: `http://localhost:8000/ping`

## Architecture

### Agent Pipeline

Four sequential agents orchestrated via `strands.multiagent.GraphBuilder`:

| Stage | Agent | Model | Purpose |
|-------|-------|-------|---------|
| 1 | **Interview** | Sonnet (planner) + Haiku (executor) | Collects FortiGate deployment requirements via interactive Q&A |
| 2 | **Design** | Sonnet | Generates 2-3 architecture options with Well-Architected evaluation |
| 3 | **IaC** | Sonnet | Produces modular CloudFormation templates with validation |
| 4 | **Documentation** | Sonnet | Generates user guide, threat model, and architecture diagram |

HITL (Human-in-the-Loop) design approval is handled at the API layer between stages 2 and 3.

### Async Processing

Long-running tasks (design, IaC, docs) are processed asynchronously:

- **Production**: SQS FIFO queues → Lambda workers
- **Local dev**: In-process background worker (no SQS needed)

The backend auto-detects the mode: if `AI_LCM_SQS_DESIGN_QUEUE_URL` is set, tasks go to SQS. Otherwise, the local worker processes them.

### Storage Backends

| Backend | Config | Use Case |
|---------|--------|----------|
| `local` | `AI_LCM_STORAGE_BACKEND=local` | Development — JSON files in `.local-data/` |
| `aws` | `AI_LCM_STORAGE_BACKEND=aws` | Production — DynamoDB metadata + S3 artifacts |

The storage interface is defined in `src/storage/protocol.py`. The factory in `src/storage/__init__.py` returns the correct backend based on config.

### Real-time Updates

- **Local**: WebSocket server at `/ws` + SSE for interview streaming
- **Production**: API Gateway WebSocket (DynamoDB streams → EventBridge Pipe → Lambda → WebSocket)

## Project Structure

```
src/
├── main.py                 # FastAPI app entry point
├── agents/                 # Agent definitions (interview, design, iac, docs)
├── services/               # Business logic & task processing
├── routes/                 # API route handlers
├── models/                 # Pydantic models
├── config/                 # Settings, auth, circuit breaker, guardrails
├── storage/                # Pluggable storage backends (local / DynamoDB+S3)
├── workers/                # Lambda handlers + local background worker
├── tools/                  # Agent tools (KB search, Well-Architected, etc.)
├── prompts/                # LLM prompt templates
├── validation/             # IaC validation (Checkov, cfn-lint, cfn-guard)
└── utils/                  # SSE helpers, YAML parsing
```

## API Reference

### Projects

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/projects` | Create project |
| `GET` | `/api/projects` | List projects |
| `GET` | `/api/projects/{id}` | Get project |
| `GET` | `/api/projects/{id}/state` | Full wizard state (frontend hydration) |
| `DELETE` | `/api/projects/{id}` | Delete project + all data |

### Interview

| Method | Path | Response | Rate Limit |
|--------|------|----------|------------|
| `POST` | `/api/interview/chat` | SSE stream | 10/min |

### Design

| Method | Path | Description | Rate Limit |
|--------|------|-------------|------------|
| `POST` | `/api/design/submit` | Submit async design task → `202` | 5/min |
| `GET` | `/api/design/task/{task_id}` | Poll task status | 30/min |
| `POST` | `/api/design/select` | Select a design option | 10/min |
| `POST` | `/api/design/refine` | Refine with deployment parameters | 10/min |

### IaC

| Method | Path | Description | Rate Limit |
|--------|------|-------------|------------|
| `POST` | `/api/iac/submit` | Submit async IaC task → `202` | 10/min |
| `GET` | `/api/iac/task/{task_id}` | Poll task status | 30/min |

### Documentation

| Method | Path | Description | Rate Limit |
|--------|------|-------------|------------|
| `POST` | `/api/docs/submit` | Submit async docs task | 5/min |
| `GET` | `/api/docs/task/{task_id}` | Poll task status | 30/min |
| `POST` | `/api/docs/regenerate-section` | Regenerate a single section | 5/min |

### Export & Health

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/export/{id}/iac.zip` | Download IaC as ZIP |
| `GET` | `/ping` | Basic health check |
| `GET` | `/health` | Deep health check (storage + Bedrock) |

All endpoints require `tenant_id` as a query parameter (local dev) or JWT claim (production with Cognito).

## Environment Variables

All variables use the `AI_LCM_` prefix. Copy `.env.sample` to `.env` and configure.

### Required

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_LCM_AWS_REGION` | `us-east-1` | AWS region for Bedrock calls |
| `AI_LCM_PRIMARY_MODEL_ID` | Claude Sonnet 4.5 | Bedrock model for design/IaC agents |
| `AI_LCM_LIGHTWEIGHT_MODEL_ID` | Claude Haiku 4.5 | Bedrock model for interview executor |
| `AI_LCM_STORAGE_BACKEND` | `local` | `local` or `aws` |

### Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_LCM_KNOWLEDGE_BASE_ID` | — | Bedrock KB ID (enables KB-grounded generation) |
| `AI_LCM_GUARDRAIL_ID` | — | Bedrock Guardrail ID |
| `AI_LCM_COGNITO_USER_POOL_ID` | — | Enables JWT auth when set |
| `AI_LCM_COGNITO_CLIENT_ID` | — | Cognito app client ID |
| `AI_LCM_SQS_DESIGN_QUEUE_URL` | — | SQS queue (falls back to local worker) |
| `AI_LCM_SQS_IAC_QUEUE_URL` | — | SQS queue (falls back to local worker) |
| `AI_LCM_SQS_DOCS_QUEUE_URL` | — | SQS queue (falls back to local worker) |
| `AI_LCM_DEBUG` | `false` | Debug logging (never `true` in production) |
| `AI_LCM_CORS_ORIGINS` | `["http://localhost:3000"]` | Allowed CORS origins |

See `.env.sample` for the full list including token limits and validation settings.

## Authentication

**With Cognito** (production): JWT Bearer token validated via JWKS. Tenant ID extracted from `custom:tenant_id` claim.

**Without Cognito** (local dev): Tenant ID passed as `?tenant_id=default` query parameter.

## Resilience

- **Circuit breaker** (`config/circuit_breaker.py`): Trips after repeated Bedrock failures
- **Retry**: Tenacity 3x exponential backoff on transient errors
- **Rate limiting**: slowapi per-endpoint limits (see API reference)
- **Request size limit**: 1 MB (DoS protection)
- **Security headers**: X-Content-Type-Options, X-Frame-Options, HSTS, Permissions-Policy

## Testing

```bash
uv run pytest tests/ -v                    # all tests (260+)
uv run pytest tests/test_api.py -v         # API routes only
uv run pytest tests/test_agents.py -v      # agent orchestration
uv run ruff check src/ tests/              # lint
```

## Docker

```bash
# Production image
docker build -t ai-lcm-backend .
docker run -p 8000:8000 --env-file .env ai-lcm-backend

# Lambda worker image
docker build -f Dockerfile.lambda -t ai-lcm-lambda .
```

The production image uses `tini` as PID 1 for proper signal handling, runs as non-root (uid 65532), and includes Node.js for Mermaid diagram validation.
