# All entry points for the demo (IMPLEMENTATION_PLAN.md section 3.4).
# `make help` lists targets. `make check` needs no AWS and is what CI runs.

SHELL := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

# Configuration precedence: runtime env > .env > config/temporal.toml profile > code defaults.
# A profile picked in the environment (`TEMPORAL_PROFILE=kind make ...`) must beat .env.
_PROFILE_FROM_ENV := $(TEMPORAL_PROFILE)
-include .env
ifneq ($(_PROFILE_FROM_ENV),)
override TEMPORAL_PROFILE := $(_PROFILE_FROM_ENV)
endif
TEMPORAL_CONFIG_FILE ?= $(CURDIR)/config/temporal.toml
TEMPORAL_PROFILE     ?= local
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
# The local dev server is plaintext (CLI 1.9.1 otherwise tries TLS). Every other profile
# (kind, eks-pf) carries its own address and TLS settings: the .env values for the dev
# server must not reach those clients, or TEMPORAL_TLS=false would switch mTLS off.
ifeq ($(TEMPORAL_PROFILE),local)
TEMPORAL_TLS         ?= false
else
unexport TEMPORAL_TLS TEMPORAL_ADDRESS
endif

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
        worker-support-versioned register-version wci-history case-b-serverless \
        case-a-serverless tools kind-up kind-temporal kind-images kind-workers kind-aws-creds \
        certs-pull pf pf-stop kind-status kind-down cert-renew export-agentcore-cert kind-env env \
        tools-agentcore tf-static tf-plan tf-apply tf-destroy tf-output guardrails external-id \
        cfn-template aws-profile invoke-role invoke-role-delete agentcore-build agentcore-deploy \
        agentcore-status agentcore-down agentcore-sessions register-version-aws \
        agentcore-network agentcore-logs flow-report \
        check-quotas render-config pause resume eks-kubeconfig eks-env eks-status \
        eks-temporal eks-rds-ca create-db-user rds-snapshot route53-alias finance-image finance-deploy \
        finance-scale failure

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

infra-static: ## helm template + kustomize -> kubeconform -strict for everything in deploy/ (no cluster)
	scripts/infra-static.sh

tools: ## Project-local kubeconform, terraform, tflint in .tools/bin (pinned sha256; no brew)
	scripts/tools.sh

tools-agentcore: tools ## + AgentCore CLI @aws/agentcore 1.0.1 in .tools/agentcore-cli (npm, exact version)
	scripts/tools.sh agentcore

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

spikes: ## Opus 5.5 spikes: wire params, effort, structured output (live Bedrock; costs tokens)
	RUN_BEDROCK_TESTS=1 $(UV) python spikes/opus_params.py
	RUN_BEDROCK_TESTS=1 $(UV) python spikes/opus_structured_output.py

spike-nested: ## Q20 spike (v0.3 checkout only; exits 2 from v0.5 on): nested agents vs as_tool()
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

register-version: ## deployment create -> create-version -> set-current-version (MODE=--poller|--local-wci|--agentcore-auto; CLOUD=1 = from main only)
	scripts/register-version.sh $(or $(MODE),--local-wci) $(if $(filter 1,$(CLOUD)),--cloud)

wci-history: ## The dev server's WCI system Workflow for Support's version (BUILD=<build id>)
	scripts/wci-history.sh $(BUILD)

case-b-serverless: ## Case B on the versioned queue support-tq (WF=support-2077-sl)
	SUPPORT_TASK_QUEUE=support-tq scripts/demo/case-b.sh $(or $(WF),support-2077-sl)

case-a-serverless: ## Case A on the versioned queue support-tq (WF=support-1042-sl)
	SUPPORT_TASK_QUEUE=support-tq scripts/demo/case-a.sh $(or $(WF),support-1042-sl)

# --- v0.8: first AWS contact (us-east-1, default SSO profile; go-ahead G5) -------------
# Terraform per stack under infra/terraform (local state, gitignored). tf-plan writes a saved
# plan; tf-apply applies exactly that plan. Inputs via TF_VAR_* (e.g. TF_VAR_budget_email,
# TF_VAR_create_dev_wci_role=true) or a gitignored terraform.auto.tfvars.
STACK ?=

tf-static: ## terraform fmt -check, validate (-backend=false), tflint for every stack (no AWS)
	scripts/tf.sh static

tf-plan: ## terraform plan for STACK= (writes STACK/tfplan; review it; ARGS= e.g. -target=...)
	scripts/tf.sh plan $(STACK) $(ARGS)

tf-apply: ## Apply the reviewed saved plan of STACK=
	scripts/tf.sh apply $(STACK)

tf-destroy: ## Destroy STACK= (guarded; STACK=foundation = 20-platform then 10-network; DRY_RUN=1 = plan -destroy only)
	DRY_RUN=$(or $(DRY_RUN),0) scripts/tf.sh destroy $(STACK)

tf-output: ## terraform output of STACK=
	scripts/tf.sh output $(STACK)

guardrails: ## Plan 00-guardrails (budget USD 200 80%/100% + AgentCore session alarm); then make tf-apply STACK=00-guardrails
	scripts/tf.sh plan 00-guardrails

external-id: ## Create the External ID secret temporal/agentcore-external-id if absent (never in TF state)
	scripts/external-id.sh

cfn-template: ## Download + sha256-check + vendor Temporal's self-hosted invocation-role template
	scripts/cfn-template.sh

aws-profile: ## Append [profile temporal-dev-wci] to ~/.aws/config (backup first; append-only)
	scripts/aws-profile.sh

invoke-role: ## CFN invocation-role stack trusting temporal-server-wci (IRSA; v0.12); create or update (needs the runtime)
	scripts/invoke-role.sh create-or-update --temporal-role-arn "$$(scripts/tf.sh output 20-platform -raw temporal_server_wci_role_arn)"

invoke-role-delete: ## Delete the CFN invocation-role stack (Project tag checked)
	scripts/invoke-role.sh delete

agentcore-build: ## AgentCore CLI synth of the runtime (MODE=PUBLIC|VPC; local packaging check, no AWS)
	scripts/agentcore-deploy.sh build

agentcore-deploy: ## Bundle + agentcore deploy (MODE=VPC is the demo runtime, v0.12; MODE=PUBLIC the v0.8 probe, ADR-0005)
	MODE=$(or $(MODE),VPC) scripts/agentcore-deploy.sh deploy

agentcore-network: ## get-agent-runtime: networkConfiguration, role, endpoints of our runtime (redacted)
	scripts/agentcore-deploy.sh network

agentcore-logs: ## Our runtime's CloudWatch log lines (SINCE=<minutes>, default 30; redacted)
	scripts/agentcore-deploy.sh logs $(or $(SINCE),30)

flow-report: ## VPC flow logs of the AgentCore ENIs: destinations by kind (SINCE=<minutes>, default 60; AWS Q2)
	$(UV) python scripts/flow_report.py --since $(or $(SINCE),60)

agentcore-status: ## agentcore status --json (redacted)
	scripts/agentcore-deploy.sh status

agentcore-down: ## Remove the runtime and the CLI stack AgentCore-AgentcoreDemo-default
	scripts/agentcore-deploy.sh down

agentcore-sessions: ## New AgentCore sessions of our runtimes in the last 15 min (must be 0 after teardown)
	scripts/agentcore-sessions.sh

register-version-aws: ## register-version against the real AgentCore endpoint (role + External ID at run time; CLOUD=1)
	scripts/register-version.sh --agentcore-auto $(if $(filter 1,$(CLOUD)),--cloud)

# --- v0.10: the AWS foundation (10-network, 20-platform; go-ahead G5) -----------------
# kubectl/helm for EKS use .temporal/kubeconfig-eks (context agentcore-demo-eks), never
# ~/.kube/config. pause/resume/tf-destroy honour DRY_RUN=1 (read-only).
check-quotas: ## Read-only pre-flight: VPC/EIP quotas, AZ IDs, node type, endpoints, PG minor, CDKToolkit, IAM simulate
	scripts/check-quotas.sh

render-config: ## Terraform outputs -> .generated/foundation.{json,env} (gitignored; v0.11/v0.12 inputs)
	$(UV) python scripts/render-config.py

eks-kubeconfig: ## Write .temporal/kubeconfig-eks (context agentcore-demo-eks); ~/.kube/config untouched
	scripts/eks.sh kubeconfig

eks-env: ## Print the KUBECONFIG export for kubectl against EKS by hand
	@scripts/eks.sh env

eks-status: ## Nodes (AZ ID, arch), add-ons, LBC and cert-manager on EKS (redacted)
	scripts/eks.sh status

# --- v0.11: Temporal and Finance on EKS (plan v0.11; scripts/eks.sh) ------------------
# Order: eks-rds-ca, create-db-user, rds-snapshot, eks-temporal, route53-alias, then
# certs-pull/pf with TEMPORAL_PROFILE=eks-pf; finance-image, finance-deploy, nexus-endpoint.
eks-rds-ca: ## Namespace temporal + Secret rds-ca from the official us-east-1 RDS CA bundle
	scripts/eks.sh rds-ca

create-db-user: ## RDS role temporal_user: password -> Secrets Manager + k8s Secret temporal-db-secret
	scripts/create-db-user.sh

rds-snapshot: ## Manual RDS snapshot temporal-pre-<CHANGE>-<ts> before an install/upgrade (CHANGE=)
	scripts/rds-snapshot.sh $(or $(CHANGE),change)

eks-temporal: ## Chart temporal-1.7.0 on EKS: certs, values (+generated IDs), dry-run=server, install --timeout 900s, NetworkPolicies
	scripts/eks.sh temporal

route53-alias: ## temporal-frontend.temporal-customer-support.internal -> internal NLB (UPSERT; ARGS=--show|--delete)
	scripts/route53-alias.sh $(ARGS)

finance-image: ## Build linux/arm64 Finance + payments-mock images and push them to ECR
	scripts/eks.sh finance-image

finance-deploy: ## Finance (2 pods, IRSA) + payments-mock on EKS via overlays/eks (FINANCE_BEDROCK_MODEL_ID= for the us. fallback)
	scripts/eks.sh finance-deploy

finance-scale: ## Scale the EKS Finance Deployment (N=0|2)
	scripts/eks.sh scale-finance $(or $(N),2)

failure: ## Failure demo: Finance to 0 mid-Case A -> pending Nexus op retries -> back to 2 -> completes (WF=)
	scripts/demo/failure.sh $(WF)

pause: ## Nodes to 0 and stop RDS (EKS, NAT, endpoints keep billing; DRY_RUN=1 prints only)
	DRY_RUN=$(or $(DRY_RUN),0) scripts/pause.sh pause

resume: ## Start RDS and nodes back to 2 (DRY_RUN=1 prints only)
	DRY_RUN=$(or $(DRY_RUN),0) scripts/pause.sh resume

# --- v0.9: the real server on kind, then mTLS (scripts/kind.sh) -------------------------
# kubectl/helm/kind use .temporal/kubeconfig-agentcore-demo (never ~/.kube/config) and
# DOCKER_CONTEXT=colima-agentcore. KIND_TLS=1 (default) installs with values.tls.yaml.
KIND_TLS ?= 1

kind-up: ## kind cluster + cert-manager + root CA / temporal-ca ClusterIssuer + postgres
	scripts/kind.sh up

kind-temporal: ## Helm chart temporal-1.7.0, server 1.32.1 (KIND_TLS=1 mTLS | 0 plaintext)
	KIND_TLS=$(KIND_TLS) scripts/kind.sh temporal

kind-images: ## Build Finance + payments-mock images and load them into kind
	scripts/kind.sh images

kind-workers: ## Finance (2 pods) + payments-mock + NetworkPolicies (FINANCE_MODEL=scripted|bedrock)
	KIND_TLS=$(KIND_TLS) scripts/kind.sh workers

kind-aws-creds: ## Laptop SSO credentials -> Secret temporal-workers/aws-creds (Finance on Bedrock)
	scripts/kind-aws-creds.sh

certs-pull: ## Client certs (operator, agentcore-worker, laptop-support) + CA -> .certs/ (EKS: TEMPORAL_PROFILE=eks-pf -> .certs/eks/)
	CLUSTER=$(if $(filter eks-pf,$(TEMPORAL_PROFILE)),eks,kind) scripts/certs-pull.sh

pf: ## Port-forward frontend -> 127.0.0.1:7233, UI -> http://127.0.0.1:8233 (kind; EKS with TEMPORAL_PROFILE=eks-pf)
	if [[ "$(TEMPORAL_PROFILE)" == eks-pf ]]; then scripts/eks.sh pf; else scripts/kind.sh pf; fi

pf-stop: ## Stop the port-forwards (kind; EKS with TEMPORAL_PROFILE=eks-pf)
	if [[ "$(TEMPORAL_PROFILE)" == eks-pf ]]; then scripts/eks.sh pf-stop; else scripts/kind.sh pf-stop; fi

kind-status: ## Nodes, pods and certificates of the kind cluster
	scripts/kind.sh status

kind-down: ## Stop port-forwards and delete the kind cluster
	scripts/kind.sh down

kind-env: ## Print the KUBECONFIG export for kubectl by hand
	@echo "export KUBECONFIG=$(CURDIR)/.temporal/kubeconfig-agentcore-demo"

# Make targets export these themselves; a bare `temporal ...` in your shell does not, and then
# reads your own default config file and profile (macOS: ~/Library/Application Support/temporalio/temporal.toml; maybe Temporal Cloud).
env: ## Print the envconfig exports for bare `temporal` commands (eval the output in your shell)
	@echo "export TEMPORAL_CONFIG_FILE=$(TEMPORAL_CONFIG_FILE) TEMPORAL_PROFILE=$(TEMPORAL_PROFILE)"

cert-renew: ## Re-issue a certificate now (NS=temporal CERT=temporal-frontend-tls)
	scripts/cert-renew.sh $(or $(NS),temporal) $(or $(CERT),temporal-frontend-tls)

export-agentcore-cert: ## agentcore-worker cert -> Secrets Manager (CLUSTER=eks SECRET_ID=temporal/agentcore-worker-mtls CONFIRM=yes; CHECK=1 read-only)
	scripts/export-agentcore-cert.sh --cluster $(or $(CLUSTER),eks) --secret-id $(or $(SECRET_ID),temporal/agentcore-worker-mtls) \
	  $(if $(filter 1,$(CHECK)),--check,$(if $(filter yes,$(CONFIRM)),--yes))

# On kind each laptop Worker presents its own client certificate (plan v0.9: one CN per
# Worker type). The profile's default identity is `operator` (CLI and demo scripts).
ifeq ($(TEMPORAL_PROFILE),kind)
agentcore-local agentcore-local-scripted worker-support-versioned: export TEMPORAL_TLS_CLIENT_CERT_PATH = $(CURDIR)/.certs/agentcore-worker/tls.crt
agentcore-local agentcore-local-scripted worker-support-versioned: export TEMPORAL_TLS_CLIENT_KEY_PATH = $(CURDIR)/.certs/agentcore-worker/tls.key
worker-support worker-support-scripted: export TEMPORAL_TLS_CLIENT_CERT_PATH = $(CURDIR)/.certs/laptop-support/tls.crt
worker-support worker-support-scripted: export TEMPORAL_TLS_CLIENT_KEY_PATH = $(CURDIR)/.certs/laptop-support/tls.key
endif
# v0.11 hybrid demo: the laptop Support Worker on EKS (through `make pf TEMPORAL_PROFILE=eks-pf`)
# presents laptop-support from the EKS CA, as a plain Worker on support-dev-tq.
ifeq ($(TEMPORAL_PROFILE),eks-pf)
worker-support worker-support-scripted: export TEMPORAL_TLS_CLIENT_CERT_PATH = $(CURDIR)/.certs/eks/laptop-support/tls.crt
worker-support worker-support-scripted: export TEMPORAL_TLS_CLIENT_KEY_PATH = $(CURDIR)/.certs/eks/laptop-support/tls.key
agentcore-local agentcore-local-scripted worker-support-versioned: export TEMPORAL_TLS_CLIENT_CERT_PATH = $(CURDIR)/.certs/eks/agentcore-worker/tls.crt
agentcore-local agentcore-local-scripted worker-support-versioned: export TEMPORAL_TLS_CLIENT_KEY_PATH = $(CURDIR)/.certs/eks/agentcore-worker/tls.key
endif

verify: ## Run scripts/verify/$(TAG).sh, e.g. make verify TAG=v0.1-scaffold
	scripts/verify/$(TAG).sh

clean: ## Remove caches (keeps .venv and .temporal)
	rm -rf .pytest_cache .ruff_cache build .generated
	find . -name __pycache__ -type d -prune -not -path './.venv/*' -exec rm -rf {} +
