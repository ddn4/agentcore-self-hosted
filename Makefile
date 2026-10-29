# All entry points for the demo (IMPLEMENTATION_PLAN.md section 3.4).
# `make help` lists targets. `make check` needs no AWS and is what CI runs.

SHELL := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

# Configuration precedence: runtime env > .env > config/temporal.toml profile > code defaults.
-include .env
TEMPORAL_CONFIG_FILE ?= $(CURDIR)/config/temporal.toml
TEMPORAL_PROFILE     ?= local
TEMPORAL_TLS         ?= false
BEDROCK_MODEL_ID     ?= global.anthropic.claude-opus-5-5
BEDROCK_EFFORT       ?= medium
AWS_REGION           ?= us-east-1
SUPPORT_TASK_QUEUE   ?= support-dev-tq
# Never touch the global docker context: every docker/kind call uses this one.
DOCKER_CONTEXT       ?= colima-agentcore
# .env may hold a relative path; make it absolute so tools run from any cwd agree.
override TEMPORAL_CONFIG_FILE := $(abspath $(TEMPORAL_CONFIG_FILE))
export

UV  := uv run --frozen
PYTEST := $(UV) pytest

.PHONY: help setup doctor fmt lint types test test-unit test-workflow test-integration \
        test-live infra-static scrub-check scrub-history check bedrock-ping dev-server \
        dev-server-up dev-server-down dev-server-status dev-server-reset dev hello-smoke \
        verify clean worker-support worker-support-scripted case-a record-histories spikes

help: ## List targets
	@grep -hE '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) | sort | awk -F':.*## ' '{printf "  %-20s %s\n", $$1, $$2}'

setup: ## Install Python 3.12 and sync the uv workspace (all packages + dev tools)
	uv python install 3.12
	uv sync --all-packages --frozen
	@[ -f .env ] || { cp .env.example .env; echo "created .env from .env.example"; }

doctor: ## Check tool versions, Python deps and AWS identity (DOCTOR_SKIP_AWS=1 to skip AWS)
	scripts/doctor.sh

fmt: ## Format and auto-fix
	$(UV) ruff format .
	$(UV) ruff check --fix .

lint: ## ruff check + format check
	$(UV) ruff check .
	$(UV) ruff format --check .

types: ## pyright (strict on finance_a2a_api, basic elsewhere)
	$(UV) pyright

test-unit: ## Unit tests (no server)
	$(PYTEST) -m unit

test-workflow: ## Workflow tests on the time-skipping test server
	$(PYTEST) -m workflow

test-integration: ## Integration tests on a two-namespace dev server (start_local)
	$(PYTEST) -m integration

test: ## unit + workflow + integration (no AWS)
	$(PYTEST) -m "unit or workflow or integration"

test-live: ## Live Bedrock tests (needs AWS credentials; costs tokens)
	RUN_BEDROCK_TESTS=1 $(PYTEST) -m bedrock

infra-static: ## Static checks for k8s/Helm/Terraform (nothing to check until v0.9)
	@if [ -d deploy ] || [ -d infra ]; then echo "infra-static: TODO from v0.9"; exit 1; \
	 else echo "infra-static: no deploy/ or infra/ yet (added in v0.9/v0.10)"; fi

scrub-check: ## No secrets, account IDs or AWS resource IDs in the tree (gitleaks if installed)
	scripts/scrub-check.sh

scrub-history: ## Same, over the whole git history (before anything goes public)
	scripts/scrub-check.sh --history

check: lint types test infra-static scrub-check ## Everything CI runs; must be green at every tag
	@echo "make check: OK"

bedrock-ping: ## Opus 5.5 answers via global. and us. profiles with no inference params
	$(UV) python scripts/bedrock_ping.py

dev-server: ## Two-namespace dev server in the foreground (agents, agent-services; UI :8233)
	scripts/dev-server.sh run

dev-server-up: ## Start the dev server in the background and wait for both namespaces
	scripts/dev-server.sh start

dev-server-down: ## Stop the background dev server
	scripts/dev-server.sh stop

dev-server-status:
	scripts/dev-server.sh status

dev-server-reset: dev-server-down ## Stop the dev server and delete its database
	rm -f .temporal/dev.db .temporal/dev.db-*

dev: ## All local processes via honcho (Procfile.dev); Ctrl-C stops them
	$(UV) honcho -f Procfile.dev start

hello-smoke: ## Run each service's HelloWorkflow on the running dev server via envconfig
	$(UV) python scripts/hello_smoke.py

worker-support: ## Support Worker (namespace agents, queue SUPPORT_TASK_QUEUE); Ctrl-C to stop
	$(UV) python -m support_agent.worker

worker-support-scripted: ## Same Support Worker with a scripted model (offline crash demo; no AWS)
	$(UV) python scripts/scripted_worker.py

case-a: ## Case A ticket #1042 to the Support Supervisor; waits for the reply (WF=support-1042)
	scripts/demo/case-a.sh $(or $(WF),support-1042)

record-histories: ## Save a completed Workflow's history for the replay test (WF=support-1042)
	$(UV) python scripts/record_history.py $(or $(WF),support-1042)

spikes: ## Opus 5.5 spikes: wire params, effort, structured output (live Bedrock; costs tokens)
	RUN_BEDROCK_TESTS=1 $(UV) python spikes/opus_params.py
	RUN_BEDROCK_TESTS=1 $(UV) python spikes/opus_structured_output.py

verify: ## Run scripts/verify/$(TAG).sh, e.g. make verify TAG=v0.1-scaffold
	scripts/verify/$(TAG).sh

clean: ## Remove caches (keeps .venv and .temporal)
	rm -rf .pytest_cache .ruff_cache build .generated
	find . -name __pycache__ -type d -prune -not -path './.venv/*' -exec rm -rf {} +
