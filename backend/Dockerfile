# ---- Stage 1: Builder — install dependencies with uv ----
# Pinned to SHA256 digest for supply chain security.
# To update: docker pull python:3.12-slim-bookworm && docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim-bookworm
FROM python@sha256:73dbd1a2ad74137451593a8ac30f7bdd0f5010fc05fb34644190cffa7696bbf3 AS builder

COPY --from=ghcr.io/astral-sh/uv:0.7 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Phase 1: deps only (cached unless lock changes)
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Phase 2: install project with source
COPY src/ src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ---- Stage 2: Runtime — lean, non-root image ----
FROM python@sha256:73dbd1a2ad74137451593a8ac30f7bdd0f5010fc05fb34644190cffa7696bbf3

LABEL org.opencontainers.image.source="https://github.com/ai-deploy-assistant" \
      org.opencontainers.image.title="ai-deploy-backend" \
      org.opencontainers.image.description="AI Deploy Assistant backend"

# tini: PID 1 init for signal forwarding and zombie reaping (app subprocesses checkov, cfn-lint)
# nodejs: required by the Mermaid diagram validator (docs agent validate-fix loop)
RUN apt-get update && \
    apt-get install -y --no-install-recommends tini nodejs npm && \
    rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 65532 appuser && \
    useradd --uid 65532 --gid 65532 --shell /usr/sbin/nologin --no-create-home appuser

WORKDIR /app

COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --chown=appuser:appuser src/ src/
COPY --chown=appuser:appuser entrypoint.sh ./

# Mermaid diagram validator (Node.js) — used by documentation agent
COPY --chown=appuser:appuser tools/validate-mermaid/ tools/validate-mermaid/
RUN cd tools/validate-mermaid && npm ci --omit=dev && npm cache clean --force

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# ECS uses 8000 (FastAPI/uvicorn), AgentCore uses 8080
EXPOSE 8000 8080

USER appuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; port='8080' if os.environ.get('AI_DEPLOY_MODE')=='agentcore' else os.environ.get('AI_DEPLOY_PORT','8000'); urllib.request.urlopen(f'http://localhost:{port}/ping')"

ENTRYPOINT ["tini", "--"]
CMD ["./entrypoint.sh"]
