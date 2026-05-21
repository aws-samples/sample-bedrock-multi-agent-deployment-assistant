# Local Development Guide

## Prerequisites

- Python 3.12+ (via mise, pyenv, or system)
- Node.js 24+ and pnpm (mise.toml specifies node 24)
- uv (Python package manager)
- Docker (for Floci local AWS emulator)
- AWS credentials configured (for real Bedrock API calls)

## Architecture

Local development uses **Floci** — a local AWS service emulator that provides DynamoDB, S3, SQS, and Cognito without requiring a real AWS account for infrastructure. Real AWS credentials are only needed for Bedrock model invocations and Knowledge Base access.

```
Docker (Floci :4566) → DynamoDB, S3, SQS FIFO, Cognito
Backend (:8000)      → FastAPI + local async worker
Frontend (:3000)     → Next.js dev server
Notification Worker  → DynamoDB stream → WebSocket bridge
```

## Quick Start

```bash
# One command starts everything:
./dev.sh

# Or with fresh state (wipes Floci data, re-provisions):
./dev.sh --fresh
```

This script:
1. Starts Floci via Docker Compose (DynamoDB, S3, SQS, Cognito emulation)
2. Runs `scripts/setup-local.sh` to provision tables, queues, buckets, Cognito pool, and AgentCore Memory
3. Syncs Knowledge Base documents to S3 and triggers Bedrock KB ingestion (if configured)
4. Starts the FastAPI backend on http://localhost:8000
5. Starts the notification worker (DynamoDB stream → WebSocket bridge)
6. Starts the Next.js frontend on http://localhost:3000

## Configuration

### AWS Credentials for Bedrock

`dev.sh` reads these environment variables (set in your shell or a git-ignored `.env.dev` file):

| Variable | Purpose |
|----------|---------|
| `AWS_PROFILE` | AWS profile for Bedrock + KB API calls |
| `BEDROCK_KB_ID` | Bedrock Knowledge Base ID (optional — omit for local KB) |
| `BEDROCK_KB_BUCKET` | S3 bucket for KB source documents |
| `BEDROCK_KB_DATA_SOURCE` | KB data source ID (enables auto-sync) |

### Backend Environment

`scripts/setup-local.sh` auto-generates `backend/.env` with Floci endpoints and provisioned resource names. Key settings:

| Variable | Local Value |
|----------|-------------|
| `AI_DEPLOY_AWS_ENDPOINT_URL` | `http://localhost:4566` (routes DynamoDB/S3/SQS to Floci) |
| `AI_DEPLOY_AWS_REGION` | `us-west-2` |
| `AI_DEPLOY_DYNAMODB_TABLE` | `ai-deploy-table` |
| `AI_DEPLOY_S3_ARTIFACTS_BUCKET` | `ai-deploy-artifacts` |
| `AI_DEPLOY_COGNITO_USER_POOL_ID` | Auto-provisioned Floci pool |
| `AI_DEPLOY_COGNITO_CLIENT_ID` | Auto-provisioned Floci client |
| `AI_DEPLOY_SQS_DESIGN_QUEUE_URL` | Floci SQS FIFO queue |
| `AI_DEPLOY_SQS_IAC_QUEUE_URL` | Floci SQS FIFO queue |
| `AI_DEPLOY_SQS_DOCS_QUEUE_URL` | Floci SQS FIFO queue |
| `AI_DEPLOY_DEBUG` | `true` |

### Frontend Environment

Copy the example file:
```bash
cp frontend/.env.local.example frontend/.env.local
```

Contents:
```
NEXT_PUBLIC_BACKEND_URL=http://localhost:8000
NEXT_PUBLIC_WEBSOCKET_URL=ws://localhost:8000/ws
NEXT_PUBLIC_AUTH_ENABLED=true
```

## Authentication (Local)

The setup script provisions a Cognito user pool in Floci with a test user:

- **Email:** `dev@local.test`
- **Password:** `LocalDev1!`

The frontend login page authenticates against Floci's Cognito endpoint. JWTs issued by Floci are verified by the backend using Floci's JWKS endpoint.

## Storage

All storage goes through **DynamoDB + S3** (backed by Floci locally):

- **DynamoDB**: Project metadata, task state, step data (design/iac/docs results)
- **S3**: Large artifacts (templates, documentation), Knowledge Base documents, interview plan state

There is no "local file storage mode" — the same `DynamoS3ProjectStore` is used in both local and production environments.

## Async Processing

The backend uses **SQS FIFO queues** for async task processing (design, IaC, docs generation). In local dev:

1. `setup-local.sh` provisions SQS FIFO queues in Floci
2. The backend's local worker polls these queues (same code path as Lambda workers in production)
3. A separate notification worker monitors DynamoDB streams and pushes status updates via WebSocket

## Knowledge Base

Three modes (auto-selected by configuration):

| Mode | When | How |
|------|------|-----|
| **Bedrock** | `BEDROCK_KB_ID` is set in env | Real AWS Bedrock KB API with vector search |
| **Local** | `knowledge_base.local_path` in config.yaml | TF-IDF search over local markdown files |
| **Null** | Neither configured | Graceful no-op (LLM uses built-in knowledge) |

For local development without Bedrock access, the system falls back to local TF-IDF search over files in `knowledge-base/`.

## Running Individual Components

```bash
# Backend only
cd backend && uv run uvicorn src.main:app --host 127.0.0.1 --port 8000 --reload

# Frontend only
cd frontend && pnpm dev

# Tests (backend)
cd backend && uv run pytest tests/ -q

# Tests (frontend)
cd frontend && pnpm test
```

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Floci won't start | Ensure Docker is running. Try `docker compose down && ./dev.sh --fresh` |
| Backend can't reach Floci | Check `http://localhost:4566/_localstack/health` returns 200 |
| Auth fails | Run `./dev.sh --fresh` to re-provision Cognito pool and test user |
| KB search returns nothing | Ensure `knowledge-base/` has markdown files, or set `BEDROCK_KB_ID` for real KB |
| "Bedrock throttling" errors | Check your AWS profile has Bedrock model access in the configured region |
