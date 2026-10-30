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
SUPPORT_A2A_MODE     ?= tool
# v0.7: WCI=1 makes `make dev-server` / `dev-server-up` enable the dev server's WCI (local
# AgentCore emulator; WCI=aws for real AWS at v0.8). Build ID: TEMPORAL_BUILD_ID or build-id.sh.
WCI                  ?= 0
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
        verify clean worker-support worker-support-scripted case-a case-b record-histories spikes \
        spike-nested sub-agents worker-finance worker-finance-scripted worker-smoke payments-mock \
        nexus-endpoint agent-card agent-card-golden smoke task-state card spike-nexus-ts \
        timeline a2a e2e approve reject pending result approval-report \
        spike-nexus-wait build-id agentcore-local agentcore-local-scripted invoke-local \
        agentcore-ping agentcore-emulator agentcore-bundle agentcore-bundle-test \
        worker-support-versioned register-version wci-history case-b-serverless

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

case-a: ## Case A #1042: Supervisor -> sub-agents -> Finance over Nexus (WF=support-1042 SUPPORT_A2A_MODE=tool|handoff)
	scripts/demo/case-a.sh $(or $(WF),support-1042)

case-b: ## Case B #2077 ($450): starts it; Finance waits in input-required for a human (WF=support-2077 CASE_B_WAIT=1)
	scripts/demo/case-b.sh $(or $(WF),support-2077)

approve: ## Finance approver approves a task in input-required (WF=support-2077 APPROVER=dana.finance COMMENT=)
	scripts/demo/approve.sh $(or $(WF),support-2077) $(or $(APPROVER),dana.finance) "$(COMMENT)"

reject: ## Finance approver rejects a task in input-required (WF=support-2077 APPROVER=dana.finance COMMENT=)
	scripts/demo/reject.sh $(or $(WF),support-2077) $(or $(APPROVER),dana.finance) "$(COMMENT)"

pending: ## Support's side while Finance waits: status and pending Nexus operations (WF=support-2077)
	temporal workflow describe --namespace agents --workflow-id $(or $(WF),support-2077) -o json \
	  | jq '{status: .workflowExecutionInfo.status, pendingNexusOperations: [(.pendingNexusOperations // [])[] | {endpoint, service, operation, state, scheduleToCloseTimeout, scheduledTime}]}'

result: ## Wait for and print a Support conversation's reply (WF=support-2077)
	temporal workflow result --namespace agents --workflow-id $(or $(WF),support-2077)

approval-report: ## How long Support waited for Finance's approver, from both histories (WF=support-2077)
	$(UV) python scripts/approval_report.py $(or $(WF),support-2077)

sub-agents: ## What each Support sub-agent answered in a run (WF=support-1042)
	temporal workflow query --namespace agents --workflow-id $(or $(WF),support-1042) --type sub_agent_runs

worker-finance: ## Finance Worker + FinanceAgentA2A Nexus handler (agent-services, agent-services-tq)
	$(UV) python -m finance_agent.worker

worker-finance-scripted: ## Same Finance Worker with a scripted Finance agent (offline; no AWS)
	$(UV) python scripts/scripted_finance_worker.py

worker-smoke: ## No-LLM caller Worker: A2ASmokeWorkflow on nexus-smoke-tq (namespace agents)
	$(UV) python -m support_agent.smoke_worker

payments-mock: ## Mock payments system (idempotent refunds) on :8081
	$(UV) python -m payments_mock.server

agent-card: ## Render the Finance Agent Card to .generated/finance-agent-card.md
	$(UV) python scripts/render-agent-card.py

agent-card-golden: ## Refresh the committed Agent Card golden file after changing the card
	$(UV) python scripts/render-agent-card.py --out packages/finance-a2a-api/tests/finance-agent-card.golden.md

nexus-endpoint: ## Create/update Nexus endpoint finance-agent (description = Agent Card); idempotent
	scripts/nexus-endpoint.sh apply

smoke: ## A2A hop with no LLM: A2ASmokeWorkflow -> finance-agent (WF=smoke-1042 AMOUNT=40 ORDER=1042)
	scripts/demo/smoke.sh $(or $(WF),smoke-1042) $(or $(AMOUNT),40) $(or $(ORDER),1042)

card: ## Standalone Nexus Operation from the CLI: FinanceAgentA2A.get_agent_card (Q16)
	temporal nexus operation execute --namespace agents --endpoint finance-agent \
	  --service FinanceAgentA2A --operation get_agent_card \
	  --operation-id card-$$(date +%s) --input '{}'

task-state: ## The A2A task as Finance sees it: get_task in agent-services (WF=support-1042 -> a2a-task-support-1042)
	scripts/demo/task-state.sh $(or $(WF),support-1042)

timeline: ## Both sides of a Support conversation: agents, tools, the A2A hop, Finance's task (WF=support-1042)
	$(UV) python scripts/timeline.py $(or $(WF),support-1042)

a2a: ## The Supervisor's A2A view: Agent Card, exchanges with Finance, guard re-prompts (WF=support-1042)
	temporal workflow query --namespace agents --workflow-id $(or $(WF),support-1042) --type a2a -o json \
	  | jq '.queryResult[0]'

e2e: ## End-to-end Cases against the RUNNING stack of TEMPORAL_PROFILE (make dev + nexus-endpoint first)
	RUN_E2E=1 $(PYTEST) -m e2e tests/e2e

spike-nexus-ts: ## Spike: the A2A hop under the time-skipping test servers (no AWS)
	$(UV) python spikes/nexus_time_skipping.py

spike-nexus-wait: ## Spike: a 24 h approval wait behind Nexus on the time-skipping server (no AWS)
	$(UV) python spikes/nexus_long_wait_time_skipping.py

record-histories: ## Save a completed Workflow's history for the replay test (WF=support-1042)
	$(UV) python scripts/record_history.py $(or $(WF),support-1042)

spikes: ## Opus 5.5 spikes: wire params, effort, structured output, nested agents (live Bedrock; costs tokens)
	RUN_BEDROCK_TESTS=1 $(UV) python spikes/opus_params.py
	RUN_BEDROCK_TESTS=1 $(UV) python spikes/opus_structured_output.py
	RUN_BEDROCK_TESTS=1 $(UV) python spikes/nested_agents.py --live

spike-nested: ## Q20 spike offline: nested agents as tools vs Strands as_tool() (no AWS)
	$(UV) python spikes/nested_agents.py

# --- v0.7: the serverless (AgentCore) shape --------------------------------------------
# The Build ID is computed in the recipe (not at parse time): build-id.sh refuses a dirty tree.
BUILD_ID_SH = $${TEMPORAL_BUILD_ID:-$$(scripts/build-id.sh)}

build-id: ## Support's content-hash Build ID (refuses uncommitted Support/contract changes)
	@scripts/build-id.sh

agentcore-local: ## Support Worker in AgentCore shape on :8080 (/invocations, /ping); real model
	TEMPORAL_BUILD_ID="$(BUILD_ID_SH)" $(UV) python -m support_agent.agentcore_app

agentcore-local-scripted: ## Same AgentCore app with a scripted model (offline; no AWS)
	TEMPORAL_BUILD_ID="$(BUILD_ID_SH)" $(UV) python scripts/scripted_agentcore_app.py

invoke-local: ## "I am the WCI": POST /invocations {deploymentName, buildId} to :8080, then /ping
	scripts/invoke-local.sh

agentcore-ping: ## /ping of the local AgentCore app (Healthy | HealthyBusy)
	@curl -sS $${AGENTCORE_URL:-http://127.0.0.1:8080}/ping; echo

agentcore-emulator: ## Local AgentCore API for the dev server's WCI (make dev-server WCI=1) -> :8080
	$(UV) python scripts/agentcore_emulator.py

agentcore-bundle: ## CodeZip code location in build/agentcore (flat; vendored contract; BUILD_ID)
	scripts/agentcore-bundle.sh

agentcore-bundle-test: ## Bundle imports and serves in a fresh 3.12 venv; arm64 zip < 250 MB
	scripts/agentcore-bundle-test.sh

worker-support-versioned: ## Long-lived versioned Support Worker (support-supervisor:<build>, support-tq)
	TEMPORAL_BUILD_ID="$(BUILD_ID_SH)" $(UV) python -m support_agent.worker_versioned

register-version: ## deployment create -> create-version -> set-current-version (MODE=--poller|--local-wci)
	scripts/register-version.sh $(or $(MODE),--local-wci)

wci-history: ## The dev server's WCI system Workflow for Support's version (BUILD=<build id>)
	scripts/wci-history.sh $(BUILD)

case-b-serverless: ## Case B on the versioned queue support-tq (WF=support-2077-sl)
	SUPPORT_TASK_QUEUE=support-tq scripts/demo/case-b.sh $(or $(WF),support-2077-sl)

verify: ## Run scripts/verify/$(TAG).sh, e.g. make verify TAG=v0.1-scaffold
	scripts/verify/$(TAG).sh

clean: ## Remove caches (keeps .venv and .temporal)
	rm -rf .pytest_cache .ruff_cache build .generated
	find . -name __pycache__ -type d -prune -not -path './.venv/*' -exec rm -rf {} +
