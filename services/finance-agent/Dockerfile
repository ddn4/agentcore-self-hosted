# syntax=docker/dockerfile:1
# Team Finance's Worker image (runbook 7b.5; plan 2.2 "the Finance Dockerfile copies
# finance_a2a_api in"). Build context: the repo root (the uv workspace), e.g.
#   docker build -f services/finance-agent/Dockerfile --target finance -t finance-agent .
# Targets:
#   finance           the real Worker: python -m finance_agent.worker (Opus 5.5 on Bedrock)
#   finance-scripted  + demo-testing and scripts/scripted_finance_worker.py: the same Worker
#                     with the scripted model (offline kind clusters; test seam only)
# Shape follows samples-python gcp/cloud_run/Dockerfile (uv sync into /app/.venv in a
# builder stage, slim runtime, non-root user). arm64 on Apple silicon / Graviton.
ARG PYTHON_IMAGE=python:3.12.12-slim-bookworm

FROM ${PYTHON_IMAGE} AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.15 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# The whole workspace definition is needed to resolve the lockfile; only the members
# installed below end up in the venv.
COPY pyproject.toml uv.lock ./
COPY packages ./packages
COPY services ./services
RUN uv sync --frozen --no-dev --no-editable --package finance-agent

FROM builder AS builder-scripted
RUN uv sync --frozen --no-dev --no-editable --package finance-agent --package demo-testing

FROM ${PYTHON_IMAGE} AS runtime-base
ENV PATH=/app/.venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN groupadd --system app && useradd --system --gid app --create-home app
WORKDIR /app
USER app

FROM runtime-base AS finance
COPY --from=builder /app/.venv /app/.venv
CMD ["python", "-m", "finance_agent.worker"]

FROM runtime-base AS finance-scripted
COPY --from=builder-scripted /app/.venv /app/.venv
COPY --chown=app:app scripts/scripted_finance_worker.py /app/scripts/scripted_finance_worker.py
CMD ["python", "scripts/scripted_finance_worker.py"]
