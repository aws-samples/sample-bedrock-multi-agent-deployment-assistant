# AI-LCM

AI-powered FortiGate Lifecycle Management platform. Uses a multi-agent pipeline (Strands + Amazon Bedrock) to guide users through deploying FortiGate-VM on AWS: gathering requirements, generating architecture designs, producing Infrastructure-as-Code, and creating documentation.

![Architecture](arch.png)

## Quick Start

```bash
# Install dependencies
cd backend && uv sync && cd ..
cd frontend && pnpm install && cd ..

# Configure environment
cp backend/.env.sample backend/.env
# Edit backend/.env — set AI_LCM_AWS_REGION to your Bedrock-enabled region

# Start both services
./dev.sh
```

Backend: http://localhost:8000/ping | Frontend: http://localhost:3000

See [Local Development Guide](docs/local-development.md) for detailed setup instructions.

## Architecture

```
Browser → CloudFront (static frontend)
           ↓ API calls
         ALB → ECS Fargate (FastAPI backend)
                 ↓ enqueue
               SQS FIFO queues (design / IaC / docs)
                 ↓ consume
               Lambda workers → Bedrock (LLM) → DynamoDB + S3
                                                    ↓ stream
                                                  EventBridge Pipe → Lambda → WebSocket API → Browser
```

### Agent Pipeline

Four sequential agents:

| Stage | Agent | Purpose |
|-------|-------|---------|
| 1 | **Interview** | Collects FortiGate deployment requirements via form + AI chat |
| 2 | **Design** | Generates 2-3 architecture options with Well-Architected evaluation |
| 3 | **IaC** | Produces modular CloudFormation templates with 3-layer validation |
| 4 | **Documentation** | User guide, threat model, and architecture diagram |

HITL design approval happens between stages 2 and 3 — the user selects an architecture option and provides deployment parameters before IaC generation begins.

### Async Processing

Long-running tasks (design, IaC, docs) are processed asynchronously:

- **Production**: SQS FIFO queues → Lambda workers (Docker images)
- **Local dev**: In-process background worker (no SQS needed)

Real-time status updates reach the browser via WebSocket (API Gateway in prod, local `/ws` in dev).

## Project Structure

```
ai-lcm/
├── backend/          # Python 3.12 / FastAPI / Strands Agents / Bedrock
├── frontend/         # Next.js 16 / React 19 / Tailwind CSS 4 / TypeScript 5
├── infra/            # AWS CDK v2 / TypeScript (12 custom constructs)
├── docs/             # Development and deployment guides
└── dev.sh            # Local development launcher
```

Each folder has its own README with detailed architecture and API documentation:

- [`backend/README.md`](backend/README.md) — Agent pipeline, API reference, environment variables, storage backends
- [`frontend/README.md`](frontend/README.md) — Wizard flow, state management, component structure
- [`infra/README.md`](infra/README.md) — CDK stack architecture, constructs, cost estimate, security

## Prerequisites

| Tool | Version | Purpose |
|------|---------|---------|
| Python | 3.12+ | Backend runtime |
| [uv](https://docs.astral.sh/uv/) | latest | Python package manager |
| Node.js | 22+ | Frontend runtime |
| [pnpm](https://pnpm.io/) | 9+ | Frontend package manager |
| AWS CLI | v2 | AWS credentials & Bedrock access |
| AWS CDK CLI | latest | Infrastructure deployment (production only) |

## Environment Variables

All backend variables use the `AI_LCM_` prefix. Configured in `backend/.env`.

### Core

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_LCM_AWS_REGION` | `us-east-1` | AWS region for Bedrock calls |
| `AI_LCM_PRIMARY_MODEL_ID` | Claude Sonnet 4.5 | Model for design, IaC, interview planner |
| `AI_LCM_LIGHTWEIGHT_MODEL_ID` | Claude Haiku 4.5 | Model for interview executor, docs |
| `AI_LCM_STORAGE_BACKEND` | `local` | `local` (JSON files) or `aws` (DynamoDB + S3) |
| `AI_LCM_DEBUG` | `false` | Debug logging and relaxed CORS |

### Optional

| Variable | Description |
|----------|-------------|
| `AI_LCM_KNOWLEDGE_BASE_ID` | Bedrock KB for FortiGate reference docs |
| `AI_LCM_GUARDRAIL_ID` | Bedrock Guardrail ID |
| `AI_LCM_COGNITO_USER_POOL_ID` | Enables JWT auth |
| `AI_LCM_COGNITO_CLIENT_ID` | Cognito app client |
| `AI_LCM_SQS_DESIGN_QUEUE_URL` | SQS queue (omit for local worker) |
| `AI_LCM_SQS_IAC_QUEUE_URL` | SQS queue (omit for local worker) |
| `AI_LCM_SQS_DOCS_QUEUE_URL` | SQS queue (omit for local worker) |

See [`backend/.env.sample`](backend/.env.sample) for the full list including token limits and validation settings.

## API Reference

### Projects

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/projects` | Create project |
| `GET` | `/api/projects` | List projects |
| `GET` | `/api/projects/{id}` | Get project |
| `GET` | `/api/projects/{id}/state` | Full wizard state (frontend hydration) |
| `DELETE` | `/api/projects/{id}` | Delete project + all data |

### Agent Endpoints

| Method | Path | Description | Rate Limit |
|--------|------|-------------|------------|
| `POST` | `/api/interview/chat` | AI requirements refinement (SSE) | 10/min |
| `POST` | `/api/design/submit` | Submit design generation task | 5/min |
| `GET` | `/api/design/task/{task_id}` | Poll design task status | 30/min |
| `POST` | `/api/design/select` | Select architecture option | 10/min |
| `POST` | `/api/design/refine` | Submit deployment parameters | 10/min |
| `POST` | `/api/iac/submit` | Submit IaC generation task | 10/min |
| `GET` | `/api/iac/task/{task_id}` | Poll IaC task status | 30/min |
| `POST` | `/api/docs/submit` | Submit documentation task | 5/min |
| `GET` | `/api/docs/task/{task_id}` | Poll docs task status | 30/min |
| `POST` | `/api/docs/regenerate-section` | Regenerate a single section | 5/min |
| `GET` | `/api/export/{id}/iac.zip` | Download IaC as ZIP | 5/min |

### Health

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/ping` | Basic health check |
| `GET` | `/health` | Deep health check (storage + Bedrock) |

All endpoints accept `tenant_id` as a query parameter (local dev) or JWT claim (production).

## Multi-tenancy

Projects are namespaced by `tenant_id` (defaults to `"default"`). All storage operations and S3 paths include `tenant_id`. When Cognito is configured, `tenant_id` is extracted from the JWT `custom:tenant_id` claim.

## Testing

```bash
# Backend (594 tests)
cd backend && uv run pytest tests/ -v && uv run ruff check src/ tests/

# Frontend
cd frontend && pnpm lint

# Infrastructure (24 CDK assertions)
cd infra && npm run build && npm run test
```

## Deployment

- [Local Development](docs/local-development.md) — get running in 5 minutes
- [AWS Deployment](docs/aws-deployment.md) — full 0-to-production guide with CDK
