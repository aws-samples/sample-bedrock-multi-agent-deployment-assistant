# Local Development Guide

## Prerequisites

- Python 3.12+ (via mise, pyenv, or system)
- Node.js 20+ and pnpm
- uv (Python package manager)
- AWS credentials configured (for Bedrock access — optional for local KB mode)

## Initial Setup

```bash
# Clone the repo
git clone <repo-url> && cd ai-deploy-assistant

# Backend
cd backend
uv sync                    # Creates .venv and installs all dependencies
cp .env.sample .env        # Create local env config
cd ..

# Frontend
cd frontend
pnpm install
cd ..
```

## Environment Configuration

Edit `backend/.env`:

```bash
# Minimum required for local development
AI_DEPLOY_AWS_REGION=us-east-1
AI_DEPLOY_STORAGE_BACKEND=local          # File-based storage (no DynamoDB/S3 needed)
AI_DEPLOY_KNOWLEDGE_BASE_ID=             # Leave empty to use local KB
AI_DEPLOY_DEBUG=true                     # Enables permissive CORS for localhost
```

### With Bedrock (recommended for full experience)

```bash
AI_DEPLOY_AWS_REGION=us-east-1
AI_DEPLOY_PRIMARY_MODEL_ID=us.anthropic.claude-sonnet-4-20250514-v1:0
AI_DEPLOY_LIGHTWEIGHT_MODEL_ID=us.anthropic.claude-haiku-3-20250310-v1:0
AI_DEPLOY_KNOWLEDGE_BASE_ID=YOUR_KB_ID   # Optional — Bedrock KB for grounding
```

### Without Bedrock (local KB mode)

If you don't have Bedrock access, the system uses local knowledge base documents:

```bash
AI_DEPLOY_KNOWLEDGE_BASE_ID=             # Empty — triggers local KB fallback
```

The `config.yaml` at project root points to `knowledge-base/` directory. Documents placed there are indexed and searched using TF-IDF scoring.

## Running the Application

### Both services (recommended)

```bash
./dev.sh
```

### Backend only

```bash
cd backend
uv run uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload
```

### Frontend only

```bash
cd frontend
pnpm dev
```

### Access Points

| Service | URL |
|---------|-----|
| Frontend | http://localhost:3000 |
| Backend API | http://localhost:8000 |
| Health check | http://localhost:8000/ping |
| API docs (Swagger) | http://localhost:8000/docs |

## Local Knowledge Base

The local KB is the development fallback for Bedrock. It reads markdown files from `knowledge-base/` with the same path structure as the S3 bucket:

```
knowledge-base/
  {use_case}/
    {deployment_type}/
      {document_type}.md
```

### Adding documents

1. Create a markdown file in the appropriate path
2. The `LocalKBProvider` indexes on first search (lazy loading)
3. To force re-indexing, restart the backend

### How search works locally

- **TF-IDF scoring** — term frequency x inverse document frequency
- **Path-based filtering** — `use_case` and `deployment_type` map to directory names, `document_type` maps to the filename stem
- **Results capped at 2000 chars** per document (same as Bedrock context window)

## Running Tests

```bash
cd backend

# Full test suite
AI_DEPLOY_KNOWLEDGE_BASE_ID="" uv run pytest tests/ -q

# Specific test file
uv run pytest tests/test_layer_plan.py -v

# With coverage
uv run pytest tests/ --cov=src --cov-report=term-missing
```

## Storage

In local mode (`AI_DEPLOY_STORAGE_BACKEND=local`), all data is stored in `.local-data/sessions/` as JSON files. This directory is gitignored.

In AWS mode, DynamoDB stores metadata and S3 stores artifacts.

## Async Processing

When `AI_DEPLOY_SQS_*` queue URLs are not configured (the default for local dev), the backend spawns a local worker thread that processes design, IaC, and documentation tasks synchronously. No SQS or Lambda needed.

## Common Issues

### "Knowledge base not configured"

This is normal if `AI_DEPLOY_KNOWLEDGE_BASE_ID` is empty AND no documents exist in `knowledge-base/`. The system still works — the LLM uses its built-in knowledge instead of KB grounding.

### Broken virtualenv

If the venv has dangling symlinks (e.g., after Python version change):

```bash
cd backend
rm -rf .venv
uv sync
```

### Port already in use

```bash
lsof -i :8000   # Find the process
kill <PID>       # Kill it
```
