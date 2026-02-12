#!/bin/sh
# FastAPI (ECS) startup
set -e

# Validate port is numeric
PORT="${AI_LCM_PORT:-8000}"
case "$PORT" in
    ''|*[!0-9]*) echo "ERROR: AI_LCM_PORT must be numeric, got: $PORT" >&2; exit 1 ;;
esac

exec uvicorn src.main:app \
    --host 0.0.0.0 \
    --port "$PORT" \
    --timeout-keep-alive 120 \
    --limit-max-requests 10000
