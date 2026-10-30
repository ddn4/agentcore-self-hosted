# syntax=docker/dockerfile:1
# The mock payments system (stdlib only). Build context: the repo root.
#   docker build -f services/payments-mock/Dockerfile -t payments-mock .
ARG PYTHON_IMAGE=python:3.12.12-slim-bookworm
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src \
    PAYMENTS_HOST=0.0.0.0 PAYMENTS_PORT=8080
RUN groupadd --system app && useradd --system --gid app --create-home app
WORKDIR /app
COPY --chown=app:app services/payments-mock/src ./src
USER app
EXPOSE 8080
CMD ["python", "-m", "payments_mock.server"]
