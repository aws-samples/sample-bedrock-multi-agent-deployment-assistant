# Local Development Setup

Get AI-LCM running on your machine for development.

## Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Python | 3.12+ | [python.org](https://www.python.org/downloads/) |
| uv | latest | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Node.js | 22+ | [nodejs.org](https://nodejs.org/) |
| pnpm | 9+ | `npm install -g pnpm` |
| AWS CLI | v2 | [aws.amazon.com/cli](https://aws.amazon.com/cli/) |

**AWS credentials** must be configured with Bedrock model access:

```bash
aws configure    # set your access key, secret, and region
```

You need access to Claude models in Amazon Bedrock. Go to the [Bedrock console](https://console.aws.amazon.com/bedrock/) → **Model access** → request access to **Anthropic Claude** models in your target region.

## Setup

### 1. Clone & Install

```bash
git clone <repo-url> && cd ai-lcm

# Backend
cd backend && uv sync && cd ..

# Frontend
cd frontend && pnpm install && cd ..
```

### 2. Configure Environment

```bash
cd backend
cp .env.sample .env
```

Edit `backend/.env` — the minimum config for local dev:

```ini
AI_LCM_AWS_REGION=us-east-1          # your Bedrock region
AI_LCM_STORAGE_BACKEND=local         # JSON file storage (no AWS infra needed)
AI_LCM_DEBUG=true                    # verbose logging
```

The frontend is pre-configured in `frontend/.env.local`:

```ini
NEXT_PUBLIC_BACKEND_URL=http://localhost:8000
NEXT_PUBLIC_WEBSOCKET_URL=ws://localhost:8000/ws
```

### 3. Start Development

```bash
./dev.sh
```

This starts:
- **Backend**: http://localhost:8000 (FastAPI with hot reload)
- **Frontend**: http://localhost:3000 (Next.js dev server)

Press `Ctrl+C` to stop both.

Or start them individually:

```bash
# Terminal 1
cd backend && uv run uvicorn src.main:app --host 127.0.0.1 --port 8000 --reload

# Terminal 2
cd frontend && pnpm dev
```

### 4. Verify

```bash
curl http://localhost:8000/ping
# {"status": "ok"}
```

Open http://localhost:3000 in your browser.

## Running Tests

### Backend

```bash
cd backend
uv run pytest tests/ -v                    # all tests (260+)
uv run pytest tests/test_api.py -v         # single file
uv run pytest tests/ -k "test_circuit"     # keyword filter
uv run ruff check src/ tests/              # lint
```

### Infrastructure

```bash
cd infra
npm run build && npm run test              # CDK template tests (24 assertions)
```

## Local Storage

With `AI_LCM_STORAGE_BACKEND=local`, data is stored as JSON files:

```
backend/.local-data/
└── {tenant_id}/
    └── {project_id}/
        ├── project.json
        ├── requirements.json
        ├── design.json
        ├── iac.json
        └── docs.json
```

Delete `.local-data/` to reset all project data.

## Optional: Knowledge Base

The app works without a Knowledge Base, but design quality improves with one. If you have a Bedrock KB configured:

```ini
AI_LCM_KNOWLEDGE_BASE_ID=XXXXXXXXXX
```

See [Knowledge Base Setup Guide](./knowledge-base-setup-guide.md) for ingestion details.

## Common Issues

### "No Bedrock access"

Ensure your AWS credentials have the `bedrock:InvokeModel` permission and you've requested model access in the Bedrock console for your region.

### "CORS error in browser"

Check that `AI_LCM_CORS_ORIGINS` in `backend/.env` includes `http://localhost:3000` (the default).

### "WebSocket disconnects immediately"

The local WebSocket server is at `ws://localhost:8000/ws`. Ensure `NEXT_PUBLIC_WEBSOCKET_URL` in `frontend/.env.local` matches.

### Backend won't start

```bash
cd backend && uv sync    # reinstall deps
```

If using Python 3.13+, downgrade to 3.12 — some dependencies require it.

### Frontend build fails

```bash
cd frontend && rm -rf node_modules .next && pnpm install
```
