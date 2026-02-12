#!/usr/bin/env bash
# AI-LCM local development — starts backend (port 8000) and frontend (port 3000)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
BACKEND_PID=""
FRONTEND_PID=""

cleanup() {
  echo ""
  echo "Shutting down..."
  [ -n "$FRONTEND_PID" ] && kill "$FRONTEND_PID" 2>/dev/null
  [ -n "$BACKEND_PID" ]  && kill "$BACKEND_PID"  2>/dev/null
  wait 2>/dev/null
  echo "Done."
}
trap cleanup EXIT INT TERM

# --- Backend ---
echo "==> Starting backend on http://localhost:8000"
cd "$ROOT/backend"
uv run uvicorn src.main:app --host 127.0.0.1 --port 8000 --reload &
BACKEND_PID=$!

# --- Frontend ---
echo "==> Starting frontend on http://localhost:3000"
cd "$ROOT/frontend"
pnpm dev &
FRONTEND_PID=$!

# --- Wait for either to exit ---
echo ""
echo "==> Backend  : http://localhost:8000/ping"
echo "==> Frontend : http://localhost:3000"
echo "==> Press Ctrl+C to stop both"
echo ""
wait
