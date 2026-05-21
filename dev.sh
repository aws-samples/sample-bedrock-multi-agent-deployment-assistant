#!/usr/bin/env bash
# AI-Deploy local development — starts Floci, backend, notification worker, and frontend
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"

# ---------------------------------------------------------------------------
# Configuration — fill these in for full Bedrock KB integration
# Leave empty to use local file-based KB fallback
# Create a .env.dev file (git-ignored) with your values, or export them in your shell.
# ---------------------------------------------------------------------------
export BEDROCK_KB_ID="${BEDROCK_KB_ID:-}"  # e.g., "ABCDEF1234"
export BEDROCK_KB_BUCKET="${BEDROCK_KB_BUCKET:-}"  # e.g., "my-kb-source-bucket"
export BEDROCK_KB_DATA_SOURCE="${BEDROCK_KB_DATA_SOURCE:-}"  # e.g., "ZYXWVU9876" (enables auto-sync on start)
# AWS profile used for real AWS calls (Bedrock + KB sync). Also written to backend .env as AI_DEPLOY_AWS_PROFILE.
# Only export if non-empty — boto3 treats AWS_PROFILE="" as "find profile named ''" which errors.
if [ -n "${AWS_PROFILE:-}" ]; then
  export AWS_PROFILE
fi

# ---------------------------------------------------------------------------
FRESH=false
if [[ "${1:-}" == "--fresh" || "${1:-}" == "-f" ]]; then
  FRESH=true
fi

BACKEND_PID=""
FRONTEND_PID=""
NOTIFY_WORKER_PID=""

cleanup() {
  echo ""
  echo "Shutting down..."
  [ -n "$FRONTEND_PID" ]      && kill "$FRONTEND_PID"      2>/dev/null
  [ -n "$NOTIFY_WORKER_PID" ] && kill "$NOTIFY_WORKER_PID" 2>/dev/null
  [ -n "$BACKEND_PID" ]       && kill "$BACKEND_PID"       2>/dev/null
  wait 2>/dev/null
  echo "Done."
}
trap cleanup EXIT INT TERM

# --- Floci (Docker) ---
echo "==> Starting Floci..."
if ! command -v docker &>/dev/null; then
  echo "ERROR: Docker is required. Install Docker Desktop and retry."
  exit 1
fi

cd "$ROOT"
if [ "$FRESH" = true ]; then
  echo "    --fresh: killing stale processes, wiping Floci state, local data, and backend .env..."
  # Kill any leftover processes on our ports
  lsof -ti:8000 | xargs kill -9 2>/dev/null || true
  lsof -ti:3000 | xargs kill -9 2>/dev/null || true
  docker compose down 2>/dev/null || true
  rm -rf .floci-data
  rm -rf backend/.local-data
  rm -f backend/.env
  mkdir -p .floci-data
  # Signal setup-local.sh to recreate AgentCore Memory resource
  export AGENTCORE_MEMORY_FRESH=true
fi
docker compose up -d --wait 2>/dev/null || docker-compose up -d 2>/dev/null

echo "    Waiting for Floci health..."
for i in $(seq 1 30); do
  if curl -sf http://localhost:4566/_localstack/health >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! curl -sf http://localhost:4566/_localstack/health >/dev/null 2>&1; then
  echo "ERROR: Floci did not become healthy within 30s"
  exit 1
fi
echo "    Floci is ready"

# --- Provision Resources ---
echo "==> Provisioning local AWS resources..."
bash "$ROOT/scripts/setup-local.sh"

# --- Backend ---
echo "==> Starting backend on http://localhost:8000"
cd "$ROOT/backend"
uv run uvicorn src.main:app --host 127.0.0.1 --port 8000 --reload &
BACKEND_PID=$!

# Wait for backend health
echo "    Waiting for backend..."
for i in $(seq 1 30); do
  if curl -sf http://localhost:8000/ping >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

# --- Notification Worker ---
echo "==> Starting notification worker"
cd "$ROOT/backend"
uv run python -m src.workers.local_notification_worker &
NOTIFY_WORKER_PID=$!

# --- Frontend ---
echo "==> Starting frontend on http://localhost:3000"
cd "$ROOT/frontend"
pnpm dev &
FRONTEND_PID=$!

# --- Ready ---
echo ""
echo "==> All services running:"
echo "    Backend  : http://localhost:8000/ping"
echo "    Frontend : http://localhost:3000"
echo "    Floci    : http://localhost:4566"
echo "    Press Ctrl+C to stop all"
echo ""
wait
