# AI Deploy Assistant

A generalized, AI-powered product deployment assistant. Uses a multi-agent pipeline (Strands + Amazon Bedrock) to guide users through deploying any product catalog on AWS: gathering requirements via interview, generating architecture designs, producing Infrastructure-as-Code (CloudFormation), and creating documentation.

The system is **product-agnostic** — the product catalog, interview fields, deployment patterns, and validation rules are all driven by configuration files (`config.yaml` + `catalog.lock.yaml`), not hardcoded logic.

![Architecture Diagram](./arch.png)

## Quick Start

```bash
# Install dependencies
cd backend && uv sync && cd ..
cd frontend && pnpm install && cd ..

# Configure environment
cp backend/.env.sample backend/.env
# Edit backend/.env — set AI_DEPLOY_AWS_REGION to your Bedrock-enabled region

# Start both services
./dev.sh
```

Backend: http://localhost:8000/ping | Frontend: http://localhost:3000

See [Local Development Guide](docs/local-development.md) for detailed setup.

## Architecture

```
Browser → Next.js Frontend (wizard UI)
           ↓ API calls
         FastAPI Backend (orchestrator)
           ├── Interview Agent (Haiku) → gathers requirements from user
           ├── Interview Planner (Sonnet) → plans questions from KB + catalog
           ├── Design Agent (Sonnet) → generates 3 architecture options
           ├── IaC Agent (Sonnet) → produces CloudFormation templates
           └── Documentation Agent (Sonnet) → diagrams, user guide, threat model
                    ↕
              Knowledge Base (Bedrock KB or Local files)
              + Catalog Lock File (deterministic field schema)
```

## Core Concepts

### Two Config Files

| File | Purpose | Maintained by |
|------|---------|---------------|
| `config.yaml` | Product identity + KB connection + policy overrides (~10 lines) | Developer (hand-edited) |
| `catalog.lock.yaml` | Full product schema — use cases, fields, patterns, appliance config | Generated from KB, reviewed + committed |

### 4-Stage Pipeline

1. **Interview** — AI-guided requirements gathering (fields from catalog)
2. **Design** — 3 architecture options grounded in KB documents
3. **IaC** — CloudFormation generation (parameterize, compose, or generate)
4. **Documentation** — Architecture diagram, user guide, threat model

### Knowledge Base Provider

The system supports three KB modes (auto-selected by config):

| Mode | When | How |
|------|------|-----|
| **Bedrock** | `AI_DEPLOY_KNOWLEDGE_BASE_ID` is set | AWS Bedrock KB API with vector search |
| **Local** | `knowledge_base.local_path` in config.yaml | TF-IDF search over local markdown files |
| **Null** | Neither configured | Graceful no-op (LLM uses built-in knowledge) |

## Project Structure

```
├── config.yaml              # Product identity (hand-edited)
├── catalog.lock.yaml        # Generated product schema (committed)
├── knowledge-base/          # Local KB documents (dev fallback)
│   ├── realtime-inference/  # use_case/deployment_type/doc_type.md
│   ├── batch-inference/
│   └── training/
├── backend/
│   └── src/
│       ├── config/          # Settings, catalog schema, app config
│       ├── agents/          # LLM agent implementations
│       ├── models/          # Pydantic data models
│       ├── services/        # Business logic (catalog loader, KB provider, etc.)
│       ├── tools/           # Agent tools (KB search, validation)
│       ├── prompts/         # Template prompt files ({product_name} variables)
│       ├── validation/      # cfn-lint, cfn-guard, checkov pipeline
│       └── routes/          # FastAPI endpoints
├── frontend/                # Next.js wizard UI
└── infra/                   # AWS CDK infrastructure
```

## Configuration Reference

See [Configuration Guide](docs/configuration-guide.md) for full schema documentation.

## Environment Variables

All env vars use the `AI_DEPLOY_` prefix. Key variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `AI_DEPLOY_AWS_REGION` | Yes | AWS region for Bedrock and services |
| `AI_DEPLOY_KNOWLEDGE_BASE_ID` | No | Bedrock KB ID (omit for local KB) |
| `AI_DEPLOY_STORAGE_BACKEND` | No | `local` (default) or `aws` |
| `AI_DEPLOY_PRIMARY_MODEL_ID` | No | Bedrock model for design/planning |
| `AI_DEPLOY_LIGHTWEIGHT_MODEL_ID` | No | Bedrock model for interview execution |

See `backend/.env.sample` for the complete list.

## Development

```bash
# Run backend only
cd backend && uv run uvicorn src.main:app --reload

# Run tests
cd backend && uv run pytest tests/ -q

# Run frontend
cd frontend && pnpm dev
```

### Local Knowledge Base

For development without AWS Bedrock access, place documents in `knowledge-base/`:

```
knowledge-base/
  {use_case}/
    {deployment_type}/
      {document_type}.md    # architecture, sizing, configuration, etc.
```

The local KB provider indexes these files and performs TF-IDF text search with the same metadata filtering as Bedrock. Set `AI_DEPLOY_KNOWLEDGE_BASE_ID=""` to use local mode.

## Deployment

See [AWS Deployment Guide](docs/aws-deployment.md) for CDK-based production deployment.
