# Implementation Plan: Self-hosted Temporal + AgentCore Customer-Support A2A Demo

## Summary of tags

| Tag | Goal | Runs where | Key verify items |
|---|---|---|---|
| `v0.0-research` | Freeze the design runbook and its list of unknowns | — | — |
| `v0.1-scaffold` | Green repo, two-namespace dev server, `make check` in CI, Opus 5.5 ping | laptop | envconfig keys, plugin import path, `start_local` pinned CLI |
| `v0.2-durable-agent` | Supervisor `TemporalAgent` + activity tools; crash-safe LLM loop | laptop + Bedrock | structured output vs forced `tool_choice`; no inference params; effort; model pin; ScriptedModel seam |
| `v0.3-sub-agents` | Order Lookup + Return Policy as nested agents-as-tools | laptop + Bedrock | Q20 nested agents |
| `v0.4-finance-agent` | A2A contract + Finance agent in `agent-services` behind Nexus endpoint; no-LLM smoke caller | laptop + Bedrock | A2A field names, Q16 (CLI), Q21, Nexus vs time-skipping |
| `v0.5-a2a-over-nexus` | Supervisor calls Finance via an async in-workflow `@tool` over Nexus | laptop + Bedrock | **Q19a**, prompt drift |
| `v0.6-human-in-the-loop` | `input-required` + approve/reject signals + timeout budget | laptop + Bedrock | Q19b, end-to-end timeout path |
| `v0.7-serverless-shape` | Worker in AgentCore shape: invoke, drain, exit, PINNED versioning, CodeZip bundle | laptop | WCI-in-dev-server, Q13 (dev), CodeZip deps, 7.1 `create` |
| `v0.8-wci-handshake` *(optional)* | Dev server's WCI assumes the self-hosted role and invokes real AgentCore (PUBLIC probe) | laptop + AWS (cents) | Q6, 6.2 CFN params, External ID, CLI 1.0.x, Q15 (first, may be inconclusive) |
| `v0.9-temporal-on-kind-mtls` | Chart 1.7.0 / server 1.32.1 on kind, then mTLS everywhere with cert-manager | laptop (kind) | chart keys, WCI namespace constraint, Q10, Q18, Q5, NetworkPolicy |
| `v0.10-aws-foundation` ☁ | Terraform: VPC on AgentCore AZ IDs, endpoints, R53, EKS, RDS, IRSA | AWS | module majors, `force_ssl`, quotas, CDK bootstrap, IAM simulate |
| `v0.11-temporal-and-finance-on-eks` ☁ | Server on EKS (RDS, NLB, IRSA), Finance on EKS, hybrid Case B from laptop | AWS + laptop | RDS TLS, schema-job CA, NLB passthrough, global-profile IAM, Q16 on 1.32.1 |
| `v0.12-agentcore-worker` ☁ | Support on AgentCore VPC mode; WCI invokes; PingWorkflow then Case A | AWS | **Q13 definitive**, Q6, 5.4 CLI VPC runtime, AWS Q2 |
| `v0.13-scale-to-zero` ☁ | Case B fully in cloud: session drains, approval, second invocation; N=10 load | AWS | **Q15 definitive**, AWS Q8 metrics |
| `v1.0.0` | `make up` / `make down` (Phase 11 order), presenter script, ledger closed | both | Phase 11 teardown |
| `v1.1-versioned-rollout` *(post-1.0, optional)* | Ship Support v2 mid-conversation; v1 stays pinned | AWS | Phase 10 drainage, delete-endpoint flags, 2.12 |
| `v1.2-operate` *(post-1.0, optional)* | Dashboards, traces, N=50 load test, rotation drill | AWS | Nexus metric names, 6.3 rotation |

The main line has 15 tags, ending at v1.0.0. Effort is about 17–20 focused days. v0.0–v0.9 are local and take about 10 days, with about $1–5 of Bedrock/AgentCore spend. Standing AWS cost starts at v0.10.

---

## 1. Storyline

| # | Tag | Concept added | Runs on | Effort | AWS spend |
|---|---|---|---|---|---|
| 0 | `v0.0-research` | The design and its unknowns | — | 10 min | — |
| 1 | `v0.1-scaffold` | A repo you can trust | laptop | 0.5 d | Bedrock pennies |
| 2 | `v0.2-durable-agent` | An LLM loop as a Workflow that survives a crash | laptop + Bedrock | 1 d | tokens |
| 3 | `v0.3-sub-agents` | Agents as tools inside one team | laptop + Bedrock | 0.5–1 d | tokens |
| 4 | `v0.4-finance-agent` | Another team's agent behind a published A2A contract, reached over Nexus | laptop + Bedrock | 1.5 d | tokens |
| 5 | `v0.5-a2a-over-nexus` | The Supervisor asks Finance itself | laptop + Bedrock | 1 d | tokens |
| 6 | `v0.6-human-in-the-loop` | A durable `input-required`; the caller can be switched off | laptop + Bedrock | 1 d | tokens |
| 7 | `v0.7-serverless-shape` | A short-lived, versioned Worker that drains when idle ("I am the WCI") | laptop | 1.5 d | tokens |
| 8 | `v0.8-wci-handshake` *(optional)* | Temporal's WCI assumes a role and invokes real AgentCore | laptop + AWS | 0.5–1 d | cents (go-ahead required) |
| 9 | `v0.9-temporal-on-kind-mtls` | Real server via Helm, then zero trust on the wire | laptop (kind) | 2–2.5 d | ≈$0.40/mo if the SM secret path is used (go-ahead), else 0 |
| 10 | `v0.10-aws-foundation` ☁ | Greenfield VPC on AZ IDs, endpoints, private DNS, EKS, RDS | AWS | 1.5 d | **meter starts** |
| 11 | `v0.11-temporal-and-finance-on-eks` ☁ | Rehearsed values on RDS/NLB/IRSA; Finance on EKS; hybrid Case B | AWS + laptop | 2–2.5 d | standing |
| 12 | `v0.12-agentcore-worker` ☁ | Zero pollers: the WCI invokes AgentCore in VPC mode | AWS | 1.5–2 d | + sessions |
| 13 | `v0.13-scale-to-zero` ☁ | Session dies while a human decides; a second invocation finishes | AWS | 1 d | small |
| 14 | `v1.0.0` | One command up, one command down, presenter script | both | 0.5 d | — |
| — | `v1.1-versioned-rollout` *(optional)* | Ship v2 while v1 conversations finish on v1 | AWS | 1 d | small |
| — | `v1.2-operate` *(optional)* | Dashboards, traces, load test, rotation | AWS | 1 d | small |

**Arc for a talk.** "A durable agent (2) becomes a team (3). Another team publishes an agent with a contract (4), and our agent asks it over Nexus (5). That agent can wait hours for a human (6), so our caller doesn't need to stay running (7), and Temporal itself can start it on AgentCore (8). Same app on the real server, locked down (9), on AWS (10–11). Now the caller really is serverless (12) and really scales to zero while the human decides (13)."

### Why AgentCore VPC mode (v0.12) comes after EKS, and why a cheap probe (v0.8) can come before it

- **VPC mode needs things that only exist from v0.11.** It needs the Route 53 name, an internal NLB, frontend SANs, a CA-issued client cert in Secrets Manager, and the WCI running in the server's worker service under IRSA (runbook §2: `service/worker/fx.go` registers `wcicomponent.Module`).
- **Runbook 7b.9 says to prove the Nexus handler on EKS first.** v0.11 does that, so any v0.12 failure is AgentCore, IAM or WCI wiring.
- **v0.7 removes the code risk for free.** It runs the exact entrypoint, debounce, drain, `complete_async_task`, PINNED versioning and CodeZip bundle on the laptop.
- **v0.8 removes most of the IAM and CLI integration risk for cents, before any standing infrastructure.** It covers the invocation role from the self-hosted CFN template, the External ID, AgentCore CLI 1.0.x, CodeZip in AgentCore, and the WCI's history shape (Q6).
  - The PUBLIC runtime in v0.8 is a **throwaway probe**. It is never the demo path (ADR-0005).
  - It runs only if v0.7 shows that the dev server contains the WCI.
  - Q13 is **not** answered there. A local Worker is used to make registration succeed, so Q13 is answered at v0.12.
  - Q15 gets a first, possibly inconclusive answer, guarded against invocation noise.

---

## 2. Repo bootstrap and layout

### 2.1 Bootstrap (v0.0 and v0.1)

```bash
cd /Users/dan/code/demo/agentcore-self-hosted
git init -b main
printf '.DS_Store\n' > .gitignore
git add TEMPORAL_SELFHOSTED_AGENTCORE_RUNBOOK.md .gitignore
git commit -m "docs: research runbook for self-hosted Temporal 1.32 + AgentCore serverless workers"
git tag -a v0.0-research -m "Starting point: design runbook and its open questions, no code"
gh repo create <owner>/agentcore-self-hosted --private --source . --push   # ask user for owner
git push origin v0.0-research
```

- The repo stays **private** until `make scrub-check` passes on the whole history and the user reviews it before going public (§3.2).
- Install tools before v0.8 and v0.10. `terraform` and `agentcore` are not installed today.
  ```bash
  brew install terraform kubeconform tflint gitleaks cmctl
  npm i -g @aws/agentcore@1.0          # needs Node >= 20.12; then: agentcore --version
  pip show bedrock-agentcore-starter-toolkit && pip uninstall -y bedrock-agentcore-starter-toolkit   # shadows the CLI
  ```

### 2.2 Layout (final state; each tag says what it adds)

```
agentcore-self-hosted/
├── TEMPORAL_SELFHOSTED_AGENTCORE_RUNBOOK.md   # design source of truth (stays at root; ⚠️ items annotated "Resolved in vX")
├── README.md                                  # story index: tag → what it adds → how to run → talk track → Build ID
├── CHANGELOG.md                               # one section per tag
├── Makefile                                   # all entry points (§3.4)
├── Procfile.dev                               # honcho: dev-server, payments-mock, finance worker, smoke worker, support worker
├── pyproject.toml  uv.lock  .python-version (3.12)  .tool-versions (python 3.12.x, terraform 1.10+, nodejs 20.12+)
├── .env.example  .gitignore  .pre-commit-config.yaml (ruff, gitleaks, scrub-check, terraform fmt)
├── config/temporal.toml                       # envconfig profiles: local, kind, eks-pf (addresses, server names, cert PATHS only)
├── docs/
│   ├── story/<tag>.md                         # what, why now, run it, what to look at, verify items closed, Build ID, talk track
│   ├── adr/NNNN-*.md                          # see §3.6
│   ├── verify-ledger.md                       # every ⚠️ / §7 item → tag → result → fallback taken (seeded from §6)
│   ├── ask-temporal.md                        # questions/reports for Temporal (Q15 result, Q16, Q19 nexus_operation_as_tool)
│   ├── versions.md                            # tool/SDK/server/CLI/RDS-engine versions actually tested
│   └── demo-script.md                         # v1.0
├── packages/
│   ├── finance-a2a-api/                       # THE ONLY code shared between teams (module finance_a2a_api)
│   │   ├── src/finance_a2a_api/{__init__,a2a,agent_card}.py
│   │   └── tests/{test_roundtrip.py, contract.schema.json}
│   └── demo-testing/                          # test-only: ScriptedModel, env fixtures, history assertions
├── services/
│   ├── support-agent/                         # team Support → namespace agents → AgentCore
│   │   ├── src/support_agent/{config,activities,agents,workflows,smoke,ping,worker_factory,worker,
│   │   │                      agentcore_app,activity_tracker,mtls}.py
│   │   ├── src/support_agent/data/{orders.json, return_policy.md}
│   │   ├── agentcore/{agentcore.json.tmpl, aws-targets.json.tmpl}
│   │   └── tests/ (incl. histories/*.json for replay)
│   ├── finance-agent/                         # team Finance → namespace agent-services → EKS
│   │   ├── src/finance_agent/{config,workflows,activities,service_handler,payments_client,worker}.py
│   │   ├── data/{fraud,refund_history}.json
│   │   └── Dockerfile  tests/
│   └── payments-mock/                         # idempotent refund API (+ Dockerfile)
├── spikes/                                    # one file per ⚠️ (kept: they are part of the story)
├── tests/e2e/test_refund.py                   # profile-driven Cases A/B/reject/timeout (local | kind | eks-pf)
├── deploy/
│   ├── kind/{kind-config.yaml, postgres.yaml}
│   ├── temporal/{values.postgresql.yaml (from chart tag temporal-1.7.0), values.base.yaml,
│   │             values.tls.yaml, values.kind.yaml, values.eks.yaml}
│   ├── k8s/base/{cert-issuers, temporal-certs, temporal-workers, payments-mock}/  overlays/{kind, eks}/
│   └── agentcore/temporal-self-hosted-serverless-worker-agentcore-role.yaml   # vendored; URL + sha256 in README
├── infra/terraform/
│   ├── modules/irsa-role/
│   ├── 00-guardrails/    # Budgets alert + AgentCore session alarm (SNS email); applied at FIRST AWS touch; destroyed last
│   ├── 10-network/       # VPC (AZ IDs), NAT, flow logs, endpoints, SGs, R53 zone
│   ├── 20-platform/      # EKS, add-ons (LBC, cert-manager), RDS, IRSA roles, ECR
│   └── 30-agentcore-iam/ # AgentCore execution role (+ optional temporal-dev-wci role). NO CFN stack, NO External ID
├── scripts/
│   ├── doctor.sh  check-quotas.sh  dev-server.sh  bedrock_ping.py  nexus-endpoint.sh  render-agent-card.py
│   ├── build-id.sh  register-version.sh  wci-history.sh  agentcore-sessions.sh  timeline.py
│   ├── external-id.sh  invoke-role.sh  cfn-template.sh                       # Phase 6 via CLI, never Terraform
│   ├── demo/{case-a,case-b,approve,reject,task-state,failure,load}.sh        # all honour TEMPORAL_PROFILE
│   ├── invoke-local.sh  fake_wci.py (optional)
│   ├── kind-aws-creds.sh  certs-pull.sh  export-agentcore-cert.sh  render-config.py  create-db-user.sh
│   ├── scrub-check.sh  redact.sh
│   └── verify/<tag>.sh                        # observable acceptance checks per tag (output piped through redact.sh)
└── .github/workflows/ci.yml                   # make check (no AWS), gitleaks, scrub-check, kubeconform, terraform validate
```

**Layout rules**

- `finance_a2a_api` is the only code shared between teams. Each service has its own `config.py`, which pins the model and builds its own client. This duplication is deliberate.
- AgentCore CodeZip cannot resolve uv workspace path dependencies. `make agentcore-bundle` therefore builds `build/agentcore/` containing:
  - `src/support_agent/`, plus a vendored copy of `finance_a2a_api/`
  - `requirements.txt`, from `uv export --package support-agent --frozen --no-dev --no-hashes --no-emit-workspace`
  - a minimal **generated** `pyproject.toml`, whose `[project].dependencies` are read from that export
  - `BUILD_ID` (a file) and the rendered `agentcore.json`

  The v0.7 and v0.8 CodeZip check records which of the two dependency files AgentCore actually reads. The other file is then dropped.
- The Finance Dockerfile copies `finance_a2a_api` in.

---

## 3. Conventions

### 3.1 Python and model

- **Python 3.12**, which matches AgentCore `PYTHON_3_12`. One uv workspace (`uv sync --all-packages`).
- **Pins** live in `docs/versions.md` and are checked by `make doctor`:
  - `temporalio>=1.34.0`, `temporalio-strands-agents` (⚠️ confirm the package and the import path `temporalio.strands_agents`), `strands-agents`, `nexus-rpc`, `bedrock-agentcore>=1.9.1`, `boto3`, `httpx`
  - Temporal CLI **1.9.1** (embeds server 1.32.0); server image 1.32.1; chart 1.7.0; `@aws/agentcore` 1.0.x; Terraform ≥ 1.10; Node ≥ 20.12
- **Lint and types:** `ruff check` and `ruff format`. Pyright runs strict on `finance_a2a_api` and basic elsewhere.
- **pytest markers:**
  - `unit`
  - `workflow` (time-skipping)
  - `integration` (`start_local`, two namespaces)
  - `bedrock` (live model; needs `RUN_BEDROCK_TESTS=1`)
  - `cloud`
- **`make check`** runs lint, types, `unit`, `workflow`, `integration`, `infra-static` and `scrub-check`. It needs no AWS, must be green at every tag, and is what CI runs.
- **Test environment pinning.** `demo-testing` fixtures call:
  ```python
  WorkflowEnvironment.start_local(
      namespace="agents",
      dev_server_existing_path=shutil.which("temporal"),   # CI installs CLI 1.9.1 explicitly; doctor checks the version
      dev_server_extra_args=["--namespace", "agent-services"],
  )
  ```
  - Fallback: `dev_server_download_version` pinned to the 1.9.1 release (⚠️ check the expected string format).
  - A fixture smoke test asserts that both namespaces exist and that the CLI version matches `docs/versions.md`.
- **Model pinning and effort (ADR-0001).** Every service builds:
  ```python
  StrandsPlugin(models={"bedrock": lambda: BedrockModel(
      model_id=os.environ["BEDROCK_MODEL_ID"], region_name=os.environ["AWS_REGION"],
      additional_request_fields=effort_fields(os.environ.get("BEDROCK_EFFORT", "medium")))})
  ```
  - ⚠️ The exact request field for Opus 5.5 effort on Bedrock is verified in v0.2.
  - Every agent uses `TemporalAgent(..., model="bedrock")`.
  - **Never** pass `temperature`, `top_p`, `top_k` or a disabled-thinking setting.
  - Tests register a `ScriptedModel` under the same `"bedrock"` key, so the pinning mechanism is also the test seam.

### 3.2 Configuration, secrets and scrubbing

- **`.env.example` is committed:**
  ```
  TEMPORAL_CONFIG_FILE=config/temporal.toml
  TEMPORAL_PROFILE=local
  TEMPORAL_ADDRESS=127.0.0.1:7233
  TEMPORAL_TLS=false
  BEDROCK_MODEL_ID=global.anthropic.claude-opus-5-5
  BEDROCK_EFFORT=medium
  AWS_REGION=us-east-1
  SUPPORT_TASK_QUEUE=support-dev-tq
  SUPPORT_A2A_MODE=tool
  AGENTCORE_DEBOUNCE_SECONDS=20
  TEMPORAL_CONNECT_TIMEOUT_SECONDS=10
  FINANCE_APPROVAL_TIMEOUT=24h
  SUPPORT_NEXUS_SEND_TIMEOUT=25h
  ```
- **One profile selector: `TEMPORAL_PROFILE`.** Scripts and Make targets read only this variable (no `PROFILE=`). The Makefile does `-include .env` and `export`, so `TEMPORAL_CONFIG_FILE` reaches both the CLI and envconfig. Without it they would read `~/.config/temporalio/temporal.toml`.
- **Use `127.0.0.1`, not `localhost`.** `localhost` resolved to `::1` while the dev server was bound only to 127.0.0.1. Set `TEMPORAL_TLS=false` locally, because CLI 1.9.1 otherwise tries TLS.
- **Precedence:**
  1. Runtime-injected env (the k8s overlay, or AgentCore `envVars` rendered from Terraform)
  2. `.env`
  3. The `TEMPORAL_PROFILE` section of `config/temporal.toml`
  4. Code defaults
- **Secret material** exists only as:
  - Secrets Manager values read into env at startup (`support_agent.mtls`; a no-op when `TEMPORAL_MTLS_SECRET_ID` is unset)
  - mounted k8s Secrets
  - files in `.certs/`, which is gitignored
- **AWS credentials.** No AWS keys in `.env`; use the `default` SSO profile (us-east-1).
  - The dev-server WCI in v0.8 uses an `AWS_PROFILE=temporal-dev-wci` chain (`source_profile=default`, `role_arn=<temporal-dev-wci>`).
  - On kind, `make kind-aws-creds` turns `aws configure export-credentials` into a short-lived k8s Secret.
  - EKS uses IRSA.
- **Gitignored:** `.env`, `.certs/`, `build/`, `.generated/`, `.temporal/`, `*.pem`, `*.key`, `*.crt`, `.terraform/`, `*.tfstate*`, `terraform.tfvars`, the rendered `agentcore.json` and `aws-targets.json`, `.venv/`, `node_modules/`.
- **These never go into Terraform state:**
  - the root CA, which is cert-manager only (2.6)
  - the **External ID**. `scripts/external-id.sh` creates it idempotently (`openssl rand -hex 16` → Secrets Manager `temporal/agentcore-external-id`). The CFN invocation-role stack is created, updated and deleted **only** by `scripts/invoke-role.sh` (`aws cloudformation create-stack|update-stack|delete-stack`, runbook 6.3), which reads the External ID from Secrets Manager at run time.
  - the RDS `temporal_user` password (a script writes it to Secrets Manager and a k8s Secret)
- **Account IDs** come from `data.aws_caller_identity`, never from tfvars.
- **AWS tags:** provider `default_tags { project = "temporal-customer-support", managed-by = "terraform" }`. Script-created resources get the same tags, and teardown is verified by this tag.
- **Scrub before anything goes public.**
  - `make scrub-check` runs in pre-commit and CI. It greps for 12-digit account IDs, `arn:aws:…:<12 digits>:`, and `vpc-` / `subnet-` / `sg-` / `eni-` IDs, plus AgentCore runtime IDs.
  - All `scripts/verify/*.sh`, `wci-history.sh` and `timeline.py` output goes through `scripts/redact.sh` (account → `<acct>`, resource IDs → `<vpc-…>`) before it is pasted into story docs.
  - gitleaks runs alongside.

### 3.3 Fixed names (closes the open naming items in runbook §3.4; ADR-0004)

| Item | Value |
|---|---|
| Caller | Namespace `agents`; deployment `support-supervisor`. Versioned Task Queue `support-tq` (serverless-shape Workers only). Unversioned local Supervisor queue `support-dev-tq`. Smoke queue `nexus-smoke-tq` (`A2ASmokeWorkflow` only). |
| Build ID | `scripts/build-id.sh` computes a **content hash**, so docs, infra and Finance-only commits don't change it and a squash-merge doesn't change it either: `{ git rev-parse HEAD:services/support-agent; git rev-parse HEAD:packages/finance-a2a-api; uv export --package support-agent --frozen --no-dev --no-hashes \| shasum; } \| shasum \| cut -c1-7`. The script refuses if any of those paths is dirty. The git SHA is passed separately as env var `SUPPORT_GIT_SHA` (metadata only). |
| Registration rule | Dev-server versions (v0.7, v0.8) may be registered from `m/<tag>` branches. **Persistent cloud versions (v0.12+) are registered only from a commit on `main`** (`register-version.sh --cloud` checks `git merge-base --is-ancestor HEAD origin/main`). The Build ID is recorded in `docs/story/<tag>.md`. |
| AgentCore | Runtime `support_supervisor`; endpoint `b_<build7>` (matches `[a-zA-Z][a-zA-Z0-9_]{0,47}`; one-to-one with the Build ID; at most 10 per agent) |
| Handler | Namespace `agent-services`; queue `agent-services-tq`; k8s namespace `temporal-workers`; Nexus endpoint `finance-agent`; service `FinanceAgentA2A` (`get_agent_card` sync, `send_message` workflow-backed); Workflow ID `a2a-task-<context_id>` |
| Nexus backing | Workflow-backed. **The standalone-Activity variant (runbook 3.4, memory note) is proposed as descoped. This is a decision for the user to confirm in ADR-0004.** Q16 is still asked of Temporal (`docs/ask-temporal.md`). |
| Timeouts (ADR-0009) | Finance approval wait `FINANCE_APPROVAL_TIMEOUT=24h`. The caller's `send_message` `schedule_to_close_timeout` is `SUPPORT_NEXUS_SEND_TIMEOUT=25h`, which deviates from runbook 5.6's 24 h so that the caller receives an A2A `failed` rather than a `NexusOperationError`. The Supervisor also handles `NexusOperationError` explicitly (apology plus escalation). |
| Debounce | Local 20 s. **Cloud demo 30 s** (runbook default is 60 s; ADR-0006). |
| `numHistoryShards` | kind 4; EKS **512** (immutable; ADR-0006) |
| Visibility | SQL on PostgreSQL with password auth |
| Mock data | Order **1042**: headphones, $40, damaged (Case A). Order **2077**: laptop, $450, damaged (Case B). Approver `dana.finance`. |

### 3.4 Makefile targets (grow per tag)

| Tags | Targets |
|---|---|
| v0.1 | `setup` `doctor` `fmt` `lint` `test` `check` `scrub-check` `test-live` `bedrock-ping` `dev-server` `dev` (honcho) |
| v0.2–v0.6 | `worker-support` `worker-smoke` `worker-finance` `payments-mock` `nexus-endpoint` `case-a` `case-b` `approve` `reject` `task-state` `e2e` `record-histories` |
| v0.7–v0.8 | `build-id` `agentcore-local` `invoke-local` `agentcore-bundle` `agentcore-bundle-test` `worker-support-versioned` `register-version` `wci-history` `guardrails` `external-id` `cfn-template` `invoke-role` `agentcore-deploy` `agentcore-down` `agentcore-sessions` |
| v0.9 | `kind-up` `kind-down` `kind-temporal` `kind-workers` `kind-aws-creds` `certs-pull` `pf` |
| v0.10+ | `check-quotas` `tf-plan STACK=` `tf-apply STACK=` `tf-destroy STACK=` `render-config` `eks-temporal` `rds-snapshot` `finance-image` `finance-deploy` `export-agentcore-cert` `pause` `load` |
| v1.0 | `up` `demo-reset` `down` `down-all` |

### 3.5 Git and tags

- Work on branch `m/<tag>`, open a PR to `main` with `docs/story/<tag>.md` as the body, then squash-merge. Because the Build ID is a content hash, squash-merging does not break "tag X is Build ID Y".
- **Tag checklist:**
  1. `make check` is green.
  2. `scripts/verify/<tag>.sh` passes. Paste its redacted output into the story doc, together with the Build ID where relevant.
  3. Update `CHANGELOG.md` and `docs/verify-ledger.md`.
  4. `git tag -a <tag> -F docs/story/<tag>.md && git push origin <tag>`
  5. `gh release create <tag> --notes-file docs/story/<tag>.md`
- **Never move a tag.** Fix forward as `v0.N.1-…`.
- List tags with `git tag --sort=v:refname`, so v0.10 sorts after v0.9.
- Use Conventional Commits. A tag may contain several commits; the story doc lists them.

### 3.6 ADRs

| ADR | Records |
|---|---|
| 0001 | Model pinning and effort |
| 0002 | HITL enforced in code (not Strands `interrupt()`) |
| 0003 | A2A as a tool vs handoff; nested agents vs sequential steps |
| 0004 | Naming, content-hash Build ID, Nexus backing (descope pending user confirmation) |
| 0005 | AgentCore order. The PUBLIC runtime is a v0.8 probe only and never the demo path. |
| 0006 | Demo sizing deviations from the runbook: nodes m7g.large vs runbook m6i.xlarge (fallback m6i.xlarge with amd64 builds); RDS db.t4g.medium single-AZ vs db.r6g.large; 512 shards; cloud debounce 30 s vs 60 s |
| 0007 | Authorization option (c) as an accepted risk; External ID and CA kept out of Terraform state |
| 0008 | AgentCore runtime via the CLI, not Terraform |
| 0009 | Nexus timeout budget (caller 25 h > Finance 24 h) |

---

## 4. Milestones

### v0.0-research

| | |
|---|---|
| **Goal** | Freeze the starting point. |
| **Scope** | The runbook and `.gitignore` (§2.1). |
| **Acceptance** | `git show v0.0-research --stat` lists exactly those two files. |
| **Effort** | 10 min. |

**Talk track:** "Before any code we wrote down the design and the twenty-odd things we didn't know. Every later tag answers some of them."

### v0.1-scaffold

**Goal.** An empty but green repo with the full harness, a two-namespace dev server, and proof that Opus 5.5 answers.

**Why here.** "Green at every tag" can only be enforced if `make check` exists before the first line of agent code. Pre-release software needs its versions recorded on day one.

**Scope**
- Root config files (§2.2) and `config/temporal.toml` (`local` profile)
- Empty package skeletons
- `packages/demo-testing` fixtures (§3.1)
- `scripts/{doctor.sh, dev-server.sh, bedrock_ping.py, scrub-check.sh, redact.sh}`
- CI
- `docs/{versions.md, verify-ledger.md, ask-temporal.md, adr/0001,0004,0007}`
- A trivial `HelloWorkflow` test per service

**Steps**
```bash
uv init --bare   # then hand-edit workspace members: packages/*, services/*
uv add --dev ruff pyright pytest pytest-asyncio honcho
make doctor
make dev-server    # temporal server start-dev --namespace agents --namespace agent-services \
                   #   --db-filename .temporal/dev.db --ui-port 8233
temporal operator namespace list
make bedrock-ping  # converse() with no inference params; run for both global. and us. profiles
make check
```

`make doctor` checks:
- temporal 1.9.1, uv, Node ≥ 20.12, kind, helm, kubectl
- `aws sts get-caller-identity` in us-east-1
- from v0.8 on: the agentcore CLI version and that no starter-toolkit shadows it
- from v0.10 on: the CDK bootstrap stack (`aws cloudformation describe-stacks --stack-name CDKToolkit`), and `aws iam simulate-principal-policy` on the deploying principal for `iam:CreateServiceLinkedRole` (`network.bedrock-agentcore.amazonaws.com`) and `iam:PassRole`

**Acceptance**
- `make check` is green locally and in GitHub Actions. The fixture smoke test shows both namespaces and CLI 1.9.1.
- `bedrock-ping` replies for both `global.` and `us.`.
- gitleaks and scrub-check pass.
- `uv tree` shows `temporalio>=1.34.0`.

**Verify items**
- `ClientConfig.load_client_connect_config()` name (5.3)
- plugin package and import path (5.6)
- envconfig TOML keys and `TEMPORAL_CONFIG_FILE`
- `start_local` version pinning
- tool versions

**Runbook:** §2, 3.1, 3.2. **Effort:** 0.5 d.

**Talk track:** "One command gives you a two-namespace Temporal, and `make check` runs real Temporal tests in CI with no cloud access. It stays green at every tag."

### v0.2-durable-agent

**Goal.** A Support Supervisor `TemporalAgent` in `agents`, with `lookup_order` and `get_return_policy` as `activity_as_tool`, on a local unversioned Worker on `support-dev-tq`.

**Why here.** This is the core idea in its smallest form. It is also the cheapest place to settle the plugin, model pinning, effort, the Opus 5.5 constraints and the ScriptedModel seam.

**Scope**
- `support_agent/{config,activities,workflows,worker_factory,worker}.py`
- `data/*`
- `demo-testing/scripted_model.py`
- `spikes/{opus_structured_output.py, opus_params.py}`
- `tests/{test_workflow.py, test_replay.py, histories/}`
- ADR-0001

**Steps**
```bash
make worker-support
make case-a     # temporal workflow start -n agents -t support-dev-tq --type SupportSupervisorWorkflow -w support-1042 --input '"Order #1042 arrived damaged…"'
# durability: Ctrl-C the worker during an invoke_model activity, restart it, watch it resume
RUN_BEDROCK_TESTS=1 uv run python spikes/opus_structured_output.py
RUN_BEDROCK_TESTS=1 uv run python spikes/opus_params.py
make record-histories WF=support-1042
```

`spikes/opus_params.py` does three things:
- registers a botocore `before-send` event hook on the client Strands uses, and asserts that the actual Converse request has **no** `inferenceConfig.temperature/topP/topK` and no forced `toolChoice`
- checks that an explicit `temperature` is rejected with a 400
- times 3 agent turns each at `BEDROCK_EFFORT=low` and `medium`

**Acceptance**
- `support-1042` completes and mentions $40 and eligibility.
- The history shows `invoke_model` Activities interleaved with tool Activities.
- Activity input shows `global.anthropic.claude-opus-5-5`, not the Sonnet 4.6 default.
- Killing the Worker mid-run loses nothing.
- Workflow tests (ScriptedModel) and the replay test pass.
- The hook assertion passes. Per-turn latency at low and medium is recorded in `versions.md`, and this figure sets the debounce and demo pacing.

**Verify items**
- **`structured_output_model` on Opus 5.5 (3.2)** may fail on forced `tool_choice`.
  - Fallback 1: an unforced capture tool `submit_refund_request(order_id, amount_usd, reason)` with `tool_choice` auto.
  - Fallback 2: JSON text validated with pydantic. The result only feeds handoff mode in v0.5.
- No default inference params and no forced `toolChoice` (verified on the wire).
- The effort field name.
- The model pin takes effect.
- ScriptedModel can be injected through `models={…}`. Fallback: mock the plugin's model Activity by name.

**Runbook:** 3.2, 5.6. **Effort:** 1 d.

**Talk track:** "An ordinary Strands agent, except every model call and tool call is a Temporal Activity. I kill the process mid-thought, and it carries on from exactly where it stopped."

### v0.3-sub-agents

**Goal.** Order Lookup and Return Policy become Support-owned sub-agents: `TemporalAgent`s called from in-workflow `@tool`s. The Supervisor uses the runbook's `SUPERVISOR_PROMPT`, minus the Finance part.

**Why here.** This is composition inside one team, and it sets up the contrast with an agent that belongs to another team.

**Scope**
- `support_agent/agents.py` and `workflows.py`
- `spikes/nested_agents.py`
- ScriptedModel scripted per system prompt

**Acceptance**
- The history shows separate `invoke_model` sequences that can be attributed to the Supervisor and to each sub-agent.
- For 2077 the reply says the request needs escalation.
- Workflow and replay tests pass, with a re-recorded history.

**Verify items:** Q20, nested `invoke_async` inside a tool. Fallback (5.6): sequential sub-agent steps in `run()`. Record the path taken in ADR-0003.

**Runbook:** §1.4, 5.6. **Effort:** 0.5–1 d.

**Talk track:** "Support now has its own small team of agents, used as tools, in-process. Keep that in mind, because the next agent belongs to someone else."

### v0.4-finance-agent

**Goal.** The shared contract, plus the Finance Refund agent in `agent-services`, reached across namespaces by a caller with **no LLM**.
- `finance_a2a_api`: the dataclasses, `@nexusrpc.service FinanceAgentA2A` (5.6.1), and `FINANCE_AGENT_CARD` with `render_markdown()`.
- `FinanceAgentTaskWorkflow` (7b.4), **auto-approve only**: `fraud_score` and `refund_history`, then `issue_refund` on payments-mock with an idempotency key.
- `FinanceAgentA2AHandler`.
- `support_agent/smoke.py`: `A2ASmokeWorkflow` calls `get_agent_card`, then `send_message`, and returns the `A2ATask`.

**Why here.** The handler comes before the caller (runbook 10.9), and Nexus wiring stays separate from LLM behaviour.

**Scope**
- `packages/finance-a2a-api/*`, `services/finance-agent/*`, `services/payments-mock/*`
- `support_agent/smoke.py`
- `make worker-smoke` (a Worker on `nexus-smoke-tq`, also a `Procfile.dev` entry)
- `scripts/{nexus-endpoint.sh, render-agent-card.py}`
- An integration fixture with `create_nexus_endpoint`
- ADR-0004 updated with the backing decision

**Steps**
```bash
make dev                 # honcho: dev-server, payments-mock :8081, finance worker, smoke worker (nexus-smoke-tq)
make nexus-endpoint      # idempotent: temporal operator nexus endpoint create --name finance-agent \
                         #   --target-namespace agent-services --target-task-queue agent-services-tq \
                         #   --description-file .generated/finance-agent-card.md
temporal nexus operation execute -n agents --endpoint finance-agent --service FinanceAgentA2A \
  --operation get_agent_card --operation-id card-smoke --input '{}'
temporal workflow start -n agents -t nexus-smoke-tq --type A2ASmokeWorkflow -w smoke-1042 --input '40'
```

**Acceptance**
- Contract round-trip tests pass for every type through `temporalio.converter.default()`.
- The 7b.9 CLI JSON parses.
- A schema snapshot fails CI on any removed field or newly required field.
- The Agent Card markdown matches the code.
- The `smoke-1042` history shows `NexusOperationScheduled`, `Started` and `Completed`, and links to `a2a-task-smoke-1042`, which is `completed` with a `refund_confirmation` artifact.
- Sending twice with the same context produces one Finance Workflow. Calling `issue_refund` twice with one key produces one refund.
- The UI Nexus page shows the Agent Card.

**Verify items**
- A2A field names against the spec version you'll present (pin it in the package README).
- Q16 (CLI, 1.32.0). Fallback: the smoke Workflow.
- Q21: can Python attach a Nexus operation summary?
- Nexus under time-skipping. Expected: no, so use `start_local`.
- The 10 s sync-operation limit.

**Runbook:** §1.3, §1.4, 5.6.1, 7b.4, 7b.5, 7b.8, 7b.9, 10.9. **Effort:** 1.5 d.

**Talk track:** "The Finance team publishes exactly one thing, an A2A-shaped contract. No AI on the caller yet, just the hop. A Workflow in `agents` calls a named endpoint, and the server routes it into another namespace. Workers never talk to each other."

### v0.5-a2a-over-nexus

**Goal.** The Supervisor discovers the Agent Card in `run()` and calls `ask_finance_agent`, an async in-workflow `@tool` that awaits `execute_operation`. `SUPPORT_A2A_MODE=tool|handoff` keeps the runbook fallback one env var away, and both modes are tested.

**Why here.** This is the central concept of the demo, and the riskiest library question (Q19a) gets answered on the laptop.

**Scope**
- `support_agent/workflows.py` (5.6.2) and handoff mode
- `scripts/demo/{case-a,task-state}.sh`
- `tests/e2e/test_refund.py`, which reads `TEMPORAL_PROFILE` and `SUPPORT_TASK_QUEUE` from the profile
- `scripts/timeline.py`

**Acceptance**
- Case A: the caller history shows `get_agent_card`, then the sub-agents' `invoke_model`, then `send_message`, and the reply quotes `R-…`.
- Handoff mode gives the same result.
- In 5 live runs (`-m bedrock`), Finance is called every time.
- `make e2e TEMPORAL_PROFILE=local` is green.

**Verify items**
- **Q19a.** Fallback: `SUPPORT_A2A_MODE=handoff`.
- **Prompt drift.** Mitigation: a Workflow guard re-prompts once if no `send_message` happened. Last resort: handoff.
- Q21 applied, if supported.

**Runbook:** §1.4, 5.6, §6. **Effort:** 1 d.

**Talk track:** "Support can't move money, so it asks Finance: a different team's agent, through a published contract and a named endpoint. It isn't a child workflow, and only the contract is shared."

### v0.6-human-in-the-loop

**Goal.** Finance gets the following:
- a deterministic rule `if amount > 100 or risk == "HIGH"`
- the A2A task states
- `approve` and `reject` signals, and a `get_task` query
- an approval timeout (`FINANCE_APPROVAL_TIMEOUT`, default 24 h) that moves the task to `failed`

The Supervisor handles `rejected` and `failed` with an apology or escalation, and catches `NexusOperationError` explicitly. The caller's `schedule_to_close_timeout` comes from `SUPPORT_NEXUS_SEND_TIMEOUT` (25 h; ADR-0009).

**Why here.** This is the behaviour that justifies serverless: the caller can wait for hours at no cost.

**Scope**
- Finance `workflows.py` (complete 7b.4)
- `scripts/demo/{case-b,approve,reject}.sh`
- Time-skipping tests
- ADR-0002, ADR-0009

**Steps**
```bash
make case-b && make task-state WF=a2a-task-support-2077     # input-required
# stop the Support worker; wait
make approve WF=a2a-task-support-2077 APPROVER=dana.finance
make worker-support                                          # Supervisor finishes
```

**Acceptance**
- Finance time-skipping tests pass for each path:
  - $40 → completed
  - $450 → input-required → approve → completed
  - reject → rejected
  - timeout → failed
  - `RISK=HIGH` on $40 → input-required
  - the idempotency key equals the task ID
- **The end-to-end timeout path.** A `start_local` integration test with `FINANCE_APPROVAL_TIMEOUT=5s` and `SUPPORT_NEXUS_SEND_TIMEOUT=60s` shows the Supervisor receiving an A2A task in state `failed` (not a `NexusOperationError`) and replying with escalation.
- A unit test asserts `SUPPORT_NEXUS_SEND_TIMEOUT ≥ FINANCE_APPROVAL_TIMEOUT + 1h`.
- With the Support Worker **stopped**, approval completes Finance, and restarting Support finishes the conversation.
- `temporal workflow describe` shows one pending Nexus operation while waiting.
- Reject produces an apology.
- An approval after **≥ 30 min of real time** works.

**Verify items:** Q19b, that no Strands tool-execution timeout kills a long wait. Fallback: handoff.

**Runbook:** 7b.4, §1.4 states, 5.6, Phase 8 step 7. **Effort:** 1 d.

**Talk track:** "Over $100, Finance's agent says 'I need a human', and the task goes to `input-required`. I switch Support off completely. Nothing is lost, and when the approver signals, the conversation picks up again."

**Gate G1:** if Q19a or Q19b fails, switch the default to handoff before v0.7.

### v0.7-serverless-shape

**Goal.** The Support Worker in AgentCore shape, run locally:
- `BedrockAgentCoreApp` with an `@app.entrypoint` and a guard against starting a second Worker
- `add_async_task("temporal-worker")`, then `asyncio.create_task(worker.run())`, then an immediate acknowledgement
- an `ActivityTracker` that drains after `AGENTCORE_DEBOUNCE_SECONDS` of idleness
- `graceful_shutdown_timeout=120s`, with `complete_async_task` in `finally`
- a connect timeout `TEMPORAL_CONNECT_TIMEOUT_SECONDS` (fail fast)
- `mtls.py` (no-op locally) and `PingWorkflow`
- the CodeZip bundle
- the Worker config:
  ```python
  WorkerDeploymentConfig(
      version=WorkerDeploymentVersion("support-supervisor", BUILD_ID),
      use_worker_versioning=True,
      default_versioning_behavior=VersioningBehavior.PINNED,
  )
  ```
  on `support-tq`, with `BUILD_ID` from `scripts/build-id.sh`

**Why here.** This is the last purely local tag. It proves every behaviour of the AgentCore Worker contract for free.

**Scope**
- `support_agent/{agentcore_app,activity_tracker,mtls,ping}.py`
- `agentcore/agentcore.json.tmpl`
- `scripts/{build-id.sh, invoke-local.sh, fake_wci.py, register-version.sh}`
- `make worker-support-versioned agentcore-bundle agentcore-bundle-test` (fresh 3.12 venv import, under 250 MB zipped)

**Steps**
```bash
# 10-min check first (informs v0.8): does the dev server contain the WCI?
make dev-server WCI=1      # adds --dynamic-config-value workercontroller.enabled=true \
                           #   'workercontroller.compute_providers.enabled=["aws-agentcore"]' \
                           #   'workercontroller.scaling_algorithms.enabled=["no-sync"]'
temporal worker deployment --help        # create / create-version / set-current-version present?
make agentcore-local                     # :8080 /invocations, /ping
BUILD_ID=$(scripts/build-id.sh); make invoke-local
temporal worker deployment create -n agents --name support-supervisor
temporal worker deployment set-current-version -n agents --deployment-name support-supervisor --build-id $BUILD_ID --yes
make case-b SUPPORT_TASK_QUEUE=support-tq      # worker drains after 20 s while Finance waits
make approve …; temporal task-queue describe -n agents --task-queue support-tq   # backlog > 0, no pollers
make invoke-local                              # "I am the WCI": session #2 finishes
# after a drain, re-run set-current-version: refused without --allow-no-pollers?
make agentcore-bundle && (cd build/agentcore && agentcore build)   # packaging check, if 1.0.x has it
```

**Local-difference note** (story doc and ledger). `start-dev` accepts only `--dynamic-config-value KEY=VALUE`: there is no dynamic-config file flag and no constraint syntax, as checked with `--help`. So `workercontroller.enabled` applies to **every** namespace locally. The runbook's "WCI only for `agents`" constraint is first verified on kind in v0.9.

**Acceptance**
- `/ping` reports HealthyBusy while the Worker runs and Healthy after it drains.
- Case B shows two Worker lifetimes, pinned to one Build ID.
- Unit tests pass:
  - debounce, with a fake clock
  - a second invocation doesn't start a second Worker
  - `complete_async_task` runs on normal exit, on exception, and on connect timeout
- An integration test runs `PingWorkflow` on a versioned Worker.
- The bundle imports in a fresh venv.
- `build-id.sh` gives the same ID on the branch and after a docs-only commit.
- The result of the WCI-in-dev-server check is recorded.

**Verify items**
- Q13 on the dev server (informational only)
- 7.1: is `create` required? (A probe suggests yes.)
- Q4, the minimum `temporalio`
- CodeZip dependency file. Fallback: a Container build (arm64).
- AgentCore CLI 1.0.x commands
- whether the dev server contains the WCI

**Runbook:** §1.2, 5.1–5.5, 7.1, 7.2, §6. **Effort:** 1.5 d.

**Talk track:** "Support's Worker is now built to die. It starts when invoked, works, notices it's idle, drains and exits. When Finance answers, a new invocation picks the conversation up, pinned to the exact build. For now I'm the one invoking it, with curl."

### v0.8-wci-handshake *(optional, recommended; first AWS contact; go-ahead G5 required)*

**Goal.** The dev server's WCI assumes the self-hosted invocation role, checks the External ID, resolves the endpoint and calls `InvokeAgentRuntime` on a throwaway **PUBLIC** runtime. The Worker inside AgentCore can't reach `127.0.0.1` and is designed to fail fast. A local versioned Worker with the same Build ID makes registration succeed.

**Why here and when to skip.**
- It moves IAM, CFN, External ID, CLI and CodeZip risk ahead of any standing infrastructure.
- It runs **only if** v0.7 showed that the dev server contains the WCI. Otherwise its items move to v0.12 and v0.13.
- The PUBLIC runtime is a probe only (ADR-0005).

**Scope**
- `infra/terraform/00-guardrails`
- `infra/terraform/30-agentcore-iam` (execution role, plus `temporal-dev-wci` with `create_dev_wci_role=true`)
- `scripts/{external-id.sh, cfn-template.sh, invoke-role.sh, wci-history.sh, agentcore-sessions.sh}`
- The vendored CFN template with its sha256
- `agentcore.json.tmpl` PUBLIC variant
- ADR-0005

**Steps**
0. **Guardrails before any spend.** `make guardrails` applies `00-guardrails`:
   - an AWS Budgets alert
   - a CloudWatch alarm on AgentCore session count in the `bedrock-agentcore` namespace (⚠️ metric name, AWS Q8)

   Until the metric is confirmed, `scripts/agentcore-sessions.sh` (a CloudWatch metric query per runtime) is the check of record.
1. `make external-id` (idempotent: create the secret if absent) and `make cfn-template` (download the template, check its sha256, vendor it).
2. `make tf-apply STACK=30-agentcore-iam TF_VAR_create_dev_wci_role=true`. This creates:
   - the AgentCore execution role (Bedrock scoped to the model/profile ARNs, logs, Secrets Manager read on `temporal/agentcore-worker-mtls-*`)
   - **`temporal-dev-wci`**, trusted by the account with an `ArnLike aws:PrincipalArn` condition on your SSO role, and allowed only `sts:AssumeRole` on `Temporal-AgentCore-Worker`. It mirrors `temporal-server-wci` on EKS.

   Then add an `~/.aws/config` profile `temporal-dev-wci` (`source_profile=default`, `role_arn=…`).
3. `scripts/invoke-role.sh create --temporal-role-arn $(terraform -chdir=infra/terraform/30-agentcore-iam output -raw dev_wci_role_arn)`. The script reads the External ID from Secrets Manager at run time.
4. Restart the dev server with `AWS_PROFILE=temporal-dev-wci make dev-server WCI=1`.
   - ⚠️ If the WCI's Go SDK doesn't follow the profile chain, run `eval $(aws configure export-credentials --profile temporal-dev-wci --format env)` first. That gives 1 h credentials.
5. `make agentcore-bundle agentcore-deploy MODE=PUBLIC`, using the existing execution role from step 2 (fallback: an inline policy on the CLI-created role). Set `TEMPORAL_CONNECT_TIMEOUT_SECONDS=10`.
6. **Registration.**
   - Start `make worker-support-versioned` (same deployment and Build ID, on `support-tq`).
   - Run `register-version.sh` (`deployment create` → `create-version --aws-agentcore-*` → `set-current-version`) and record the outcome.
   - Stop the local Worker.
   - **Negative test:** `create-version` with a wrong External ID fails AssumeRole.
7. **Invocation probe.**
   - Record a WCI history baseline (`wci-history.sh`).
   - Start `PingWorkflow ping-1` with no local Worker.
   - Expect AssumeRole → External ID check → `GetAgentRuntimeEndpoint` → `InvokeAgentRuntime`. AgentCore logs show the entrypoint, a connect failure within 10 s, and `complete_async_task`.
   - Watch for 5 min and record the WCI's re-invoke cadence while the backlog exists.
   - **Terminate `ping-1`.** Confirm `temporal task-queue describe -n agents --task-queue support-tq` shows backlog 0, and that the invoke count stays flat for 5 min. Note the timestamp of the last invoke.
8. **Q15 probe** (⚠️ plausible, unverified).
   - Run Case B on the local versioned Worker until `input-required`. Invokes during this local run are expected noise.
   - Stop the Worker. Wait until backlog is 0 and there have been no invokes for 5 min.
   - Approve at time T.
   - Count **only** invokes after T whose timestamp follows `NexusOperationCompleted` in the `support-2077` history (within 2 min).
   - Classify the result as **yes**, **no** or **inconclusive**. Use "inconclusive" unless the timing clearly lines up.
   - Restart the local Worker to finish, then terminate any leftovers.
9. **Same-day teardown.**
   - Run `set-current-version` back to unversioned, or `delete-version`.
   - `make agentcore-down`
   - `scripts/invoke-role.sh delete`
   - `scripts/agentcore-sessions.sh`, which must show 0
   - Keep the External ID secret, `00-guardrails` and `30-agentcore-iam` (IAM is free).

**Acceptance**
- The WCI history shows AssumeRole (External ID enforced), `GetAgentRuntimeEndpoint` and `InvokeAgentRuntime`. The WCI namespace, ID format and activity names are recorded.
- The `create-version` outcome is recorded (with a local Worker present it is expected to succeed; if it hangs, record that).
- The negative test fails as expected.
- AgentCore sessions end cleanly, and the session count returns to 0.
- The Q15 result is recorded with timestamps.
- Q13 is explicitly marked "not answered here".

**Verify items**
- Q6, against the real WCI
- 6.2 CFN parameter names
- AgentCore CLI 1.0.x `deploy`, `status`, `logs` and `destroy`
- the endpoint naming rule
- CodeZip in AgentCore
- AWS Q4 (native secret injection)
- AWS Q6 (`terraform providers schema` grep for `aws_bedrockagentcore_*`, informational; ADR-0008 keeps the CLI)
- Q9 (does UI 2.54.1 show the compute config?)
- Q15 (first answer)

**Cost and rollback:** cents. The budget and alarm stay permanently. If the WCI keeps re-invoking unexpectedly, terminate all Workflows on `support-tq` and run `delete-version` immediately.

**Effort:** 0.5–1 d.

**Talk track** (choose by the v0.7 result):
- **WCI present:** "The Worker Controller ships inside the Temporal server, even the dev server. Here it assumes a narrowly scoped role guarded by an External ID and starts an AgentCore session. `create-version` is our self-hosted 'Validate Connection' button."
- **WCI absent** (tag skipped): "v0.7 proved the Worker contract on the laptop. The first real contact between Temporal and AgentCore comes at v0.12, on the real server."

### v0.9-temporal-on-kind-mtls *(two commits, one tag)*

**Goal.** The real server on the laptop, then lock it down.

*Commit 1, Helm:*
- kind with in-cluster `postgres:16`
- chart `temporal-1.7.0`, images `1.32.1`, SQL visibility, namespaces from values
- WCI dynamic config **constrained to `agents`**
- Finance and payments-mock as Deployments via `overlays/kind`
- Support on the laptop in serverless shape, through a port-forward

*Commit 2, mTLS (fresh cluster):*
- **cert-manager chain:** a `SelfSigned` bootstrap issues `temporal-root-ca`, which backs the `temporal-ca` CA ClusterIssuer.
- **Server certificates:** frontend (SANs for the Route 53 name and the service names) and internode (both EKUs).
- **Client certificates, each with its own CN:** `temporal-ui`, `operator`, `eks-nexus-worker`, `agentcore-worker`, `laptop-support`.
- **`values.tls.yaml`:** internode and frontend TLS, `requireClientAuth`, `web.tls`.
- **Finance** uses `TEMPORAL_TLS_*_PATH`.
- **Support's certificate:**
  - Default, no AWS: a **local-file path** (`certs-pull` → `_PATH` vars).
  - Optional, with the user's go-ahead: the exact cloud code path, where `mtls.py` reads the Secrets Manager secret **`temporal-kind/agentcore-worker-mtls`**. This name sits outside the execution-role pattern `temporal/agentcore-worker-mtls-*`.
- **NetworkPolicy** on the Finance pods.
- **ADR-0007.**

**Why here.**
- It freezes `values.base.yaml`, `values.tls.yaml` and the kustomize base that EKS reuses.
- Chart and certificate questions get answered while reinstalling is free.
- arm64 kind proves the arm64 images that the Graviton nodes need.
- The runbook requires TLS in the first install's values. kind is the rehearsal.

**Scope**
- `deploy/kind/*`
- `deploy/temporal/{values.postgresql.yaml, values.base.yaml, values.tls.yaml, values.kind.yaml}`
- `deploy/k8s/base/*`, `overlays/kind`
- Dockerfiles
- `scripts/{kind-aws-creds.sh, certs-pull.sh, export-agentcore-cert.sh}`
- `infra-static` in CI (`helm template | kubeconform -strict`, `kustomize build | kubeconform`)
- A documented `/etc/hosts` entry `127.0.0.1 temporal-frontend.temporal-customer-support.internal`

**Steps**
```bash
# commit 1
make kind-up kind-temporal            # --dry-run=server first, then helm install --version 1.7.0
kubectl exec deploy/temporal-admintools -n temporal -- temporal operator cluster describe -o json   # shards=4
kubectl logs deploy/temporal-worker -n temporal | grep -iE 'workercontroller|wci'
make kind-aws-creds kind-workers pf nexus-endpoint TEMPORAL_PROFILE=kind
TEMPORAL_PROFILE=kind make agentcore-local invoke-local case-a case-b approve invoke-local
# commit 2: fresh cluster with base+tls+kind in ONE values set
make kind-down kind-up kind-temporal kind-workers kind-aws-creds   # re-create everything
make certs-pull && TEMPORAL_PROFILE=kind temporal operator namespace list
TEMPORAL_PROFILE=kind make nexus-endpoint                          # after the namespaces exist
TEMPORAL_PROFILE=kind make agentcore-local invoke-local case-b approve invoke-local   # local-file certs
# optional SM path (go-ahead): make guardrails (if not yet applied), then
#   scripts/export-agentcore-cert.sh --secret-id temporal-kind/agentcore-worker-mtls
#   TEMPORAL_MTLS_SECRET_ID=temporal-kind/agentcore-worker-mtls make agentcore-local invoke-local
# rotation: frontend duration 1h / renewBefore 55m
cmctl renew -n temporal temporal-frontend-tls; openssl s_client -connect 127.0.0.1:7233 -servername temporal-frontend.temporal-customer-support.internal </dev/null | openssl x509 -noout -serial
cmctl renew -n temporal-workers eks-nexus-worker-tls
```

**Acceptance**
- **Helm**
  - Pods run 1.32.1, and both namespaces come from values.
  - The WCI logs at start, and the dynamic config shows the `agents` namespace constraint.
  - A dynamic-config-only `helm upgrade` takes effect without pod restarts.
  - Cases A and B pass, with `handled_by_pod` set to a kind pod.
  - Deleting a Finance pod mid-Case B doesn't break it.
- **mTLS**
  - A client with no cert, or with a cert from a foreign CA, is rejected; the operator cert works.
  - The UI connects through `web.tls`.
  - Finance connects by service name, and Support by the Route 53 name.
  - Case B passes over mTLS.
  - An unlabeled pod can't reach 7233.
  - Rotation results are recorded.
- **G2 written decision** (`docs/story/v0.9….md` §Decision): taking the Q15 result from v0.8 (or "not yet known"), decide with the user whether to proceed to AWS spend and which Q15 fallback is pre-wired. **Gate G3:** Case B over mTLS passes.

**Verify items**
- Chart 1.7.0 with 1.32.1 (2.1, Q8)
- Values keys for replicas, resources, PDB, per-component `serviceAccount`, and the admintools and schema-job mounts (2.2)
- Frontend labels for the NetworkPolicy (7b.6)
- WCI startup logs and the namespace constraint (Phase 3)
- Dynamic config reload without restart
- arm64 images
- **Q10** (server cert hot-reload). Fallback: Reloader or a rolling restart.
- **Q18** (SDK file-cert reload). Fallback: Reloader on Finance.
- Q5 (if the SM path was run)
- `TEMPORAL_TLS_*` names
- Internode EKUs (2.5)
- kindnet NetworkPolicy enforcement. Fallback: Calico.

**Runbook:** Phase 2 (2.1–2.11), Phase 3, 5.3, 7b.2, 7b.5–7b.8, §6 TLS rows.

**Cost:** 0, or about $0.40/mo for the optional kind secret. Delete that secret at v0.12.

**Effort:** 2–2.5 d.

**Talk track:** "Same code, real server: the official chart on 1.32.1 with the values we'll use on EKS. Then every connection becomes mutual TLS from our own free CA, and each Worker type gets its own identity. The A2A contract still hasn't changed."

### v0.10-aws-foundation ☁ *(go-ahead G5 required)*

**Goal.** Terraform for the AWS foundation.
- **`00-guardrails`**: apply if not already applied.
- **`10-network`**:
  - VPC with private subnets chosen by **AZ ID** (`use1-az1`, `use1-az2`, `use1-az4`)
  - a single NAT gateway, and flow logs
  - interface endpoints: `bedrock-agentcore`, `bedrock-agentcore-control`, `sts`, `secretsmanager`, `bedrock-runtime`, `logs`
  - an S3 gateway endpoint
  - SGs: `sg-agentcore-worker`, `sg-nlb-temporal-frontend`, `sg-vpce`, `sg-rds`
  - the Route 53 private zone `temporal-customer-support.internal`
- **`20-platform`**:
  - EKS with IRSA, a public endpoint limited to your /32, and the VPC CNI network-policy agent
  - nodes: 2–3 × **m7g.large** (ADR-0006 deviation; fallback m6i.xlarge with amd64 builds)
  - the LBC and cert-manager via `helm_release`
  - RDS PostgreSQL `engine_version = "16.6"` (Temporal-tested; recorded in `versions.md`), `db.t4g.medium`, single-AZ, encrypted, `deletion_protection=false`
  - IRSA roles `temporal-server-wci` and `agent-services-worker` (Bedrock ARNs from `var.bedrock_inference_profile`)
  - ECR repositories with `force_delete=true`

Also run `make external-id cfn-template`. Both are idempotent and **already done if v0.8 ran**, so every path has them before v0.12.

**Why here.** Behaviour, values and certificates are all proven, so infrastructure is now the only unknown.

**Steps**
```bash
make doctor check-quotas          # VPCs 4/5, EIPs 8/10, CDKToolkit stack, simulate CreateServiceLinkedRole + PassRole
make guardrails external-id cfn-template
make tf-apply STACK=10-network && make tf-apply STACK=20-platform
aws eks update-kubeconfig --name temporal --region us-east-1
scripts/verify/v0.10.sh | scripts/redact.sh
make render-config
```

The verify script checks:
- subnet AZ IDs
- endpoints available, and the zone associated
- nodes Ready; LBC and cert-manager Ready
- psql with `sslmode=require`
- IRSA trust subjects

**Acceptance**
- The verify script passes, covering the §5 "Foundation" checklist except the NLB alias.
- A second `plan` shows no changes.
- CI runs `terraform fmt -check`, `validate` and `tflint`.

**Verify items**
- the vpc and eks module majors
- RDS PG16 `force_ssl` (1.3)
- the STS endpoint (1.4)
- VPC CNI network-policy enablement
- CDK bootstrap, `CreateServiceLinkedRole` and `PassRole` (3.2)

**Runbook:** Phase 1, 3.1, 3.2, 4.1, 7b.3, §5 Foundation.

**Cost:** about $0.60/h, roughly $14–16 a day (⚠️ check current pricing).

**Pause and rollback**
- `make pause` scales nodes to 0 and stops RDS for up to 7 days. EKS, NAT and the endpoints keep billing.
- `make tf-destroy` runs in reverse stack order, after a Helm uninstall.

**Effort:** 1.5 d. The Terraform can be written on a branch during v0.4–v0.9.

**Talk track:** "One target builds a private VPC whose subnets are chosen by AZ ID because AgentCore requires it, endpoints so nothing needs the internet, private DNS, EKS, Postgres and two narrow IRSA roles. The budget alarm was in place before we spent a cent."

### v0.11-temporal-and-finance-on-eks ☁ *(two commits, one tag)*

**Goal.**

*Commit 1, server:*
- `values.eks.yaml` on top of the unchanged base and TLS values
- RDS with TLS through the `rds-ca` Secret
- an internal TCP NLB with `ip` targets and `sg-nlb-temporal-frontend`
- 512 shards
- IRSA on `server.worker.serviceAccount`, plus `AWS_REGION`
- cert-manager manifests
- a Route 53 alias

*Commit 2, Finance:*
- ECR images
- `overlays/eks`: IRSA, in-cluster address, `POD_NAME`, PDB, NetworkPolicy
- the `finance-agent` endpoint
- a **hybrid demo:** Support runs on the laptop through a port-forward with the `laptop-support` cert, as a plain Worker on **`support-dev-tq`**. `nexus-smoke-tq` stays reserved for `A2ASmokeWorkflow`.

**Why here.** "Same values, different backend", plus runbook 7b.9: prove the EKS handler, IRSA Bedrock access and the NetworkPolicy before AgentCore. It also shows the two teams deploying independently.

**Steps**
```bash
kubectl create namespace temporal                         # runbook 1.3: before secrets/certs
scripts/create-db-user.sh                                 # password → Secrets Manager + k8s Secret
kubectl create secret generic rds-ca -n temporal --from-file=rds-ca.pem=<(curl -s https://truststore.pki.rds.amazonaws.com/us-east-1/us-east-1-bundle.pem)
kubectl apply -k deploy/k8s/overlays/eks/cert-issuers && kubectl apply -k deploy/k8s/overlays/eks/temporal-certs
make rds-snapshot && make eks-temporal                    # dry-run=server, then install --timeout 900s
scripts/route53-alias.sh
kubectl run awscli --rm -it -n temporal --image=amazon/aws-cli --overrides='{"spec":{"serviceAccountName":"temporal-worker"}}' -- sts get-caller-identity
kubectl run dbg --rm -it -n temporal --image=alpine/openssl -- s_client -connect temporal-frontend.temporal-customer-support.internal:7233 -servername temporal-frontend.temporal-customer-support.internal
make certs-pull pf && TEMPORAL_PROFILE=eks-pf temporal operator namespace list
# commit 2
make finance-image finance-deploy && kubectl -n temporal-workers rollout status deploy/agent-services-worker
TEMPORAL_PROFILE=eks-pf make nexus-endpoint
TEMPORAL_PROFILE=eks-pf temporal nexus operation execute … get_agent_card
TEMPORAL_PROFILE=eks-pf SUPPORT_TASK_QUEUE=support-dev-tq make worker-support e2e
scripts/demo/failure.sh   # scale Finance to 0 → pending Nexus op retries → scale to 2 → completes
```

**Acceptance**
- **Server**
  - 1.32.1, 512 shards, schemas core v1.19 and visibility v1.14.
  - RDS connections use TLS.
  - The Route 53 name resolves to the NLB.
  - Through the NLB, `s_client` shows the cert-manager cert and a client-cert request (passthrough).
  - mTLS works from a pod in the VPC and fails without a cert.
  - The UI works through a port-forward.
  - IRSA returns `temporal-server-wci`, and `AWS_ROLE_ARN` is set in the worker pods only.
  - The WCI config is present.
- **Finance**
  - Finance shows 2 pollers.
  - Cases A and B pass, with `handled_by_pod` set to an EKS pod.
  - No static credentials are present.
  - An unlabeled pod is blocked.
  - The failure demo recovers.
- **The laptop Worker is stopped before tagging.**
- The §5 "Temporal server" and "Nexus and the EKS handler" checklists are complete.
- **Gate G4:** hybrid Case B passes.

**Verify items**
- RDS TLS and `enableHostVerification`
- the schema-job CA mount (2.2). Fallback: `useHelmHooks: false` plus manual `temporal-sql-tool` (2.3).
- NLB passthrough (2.4)
- IRSA wiring (4.1)
- global-profile IAM and SCP (3.2). Fallback: `us.` via a variable.
- VPC CNI NetworkPolicy
- Q16 on 1.32.1. Fallback: the smoke caller.
- a light Q18 re-check

**Runbook:** Phase 2, 3, 4.1, 7.0, 7b, Phase 8 step 7.5, 11.1.

**Rollback:** `nexus endpoint delete`; `kubectl delete ns temporal-workers`; `helm uninstall` (RDS persists, and a snapshot exists). The NLB adds about $0.02/h.

**Effort:** 2–2.5 d.

**Talk track:** "The values rehearsed on my laptop, now on RDS, behind an internal NLB that passes TLS straight through, at a private DNS name. Finance's agent runs on EKS with its own IAM identity, Support is still on my laptop, and the A2A call didn't change at all."

### v0.12-agentcore-worker ☁

**Goal.** Support on AgentCore in **VPC mode**, registered as a serverless Worker Deployment Version that the WCI invokes. Prove it with PingWorkflow, then Case A. Registration happens from `main` only.

**Why here.** Every dependency is proven. With v0.8, most of the IAM and CLI behaviour was already rehearsed, so the new parts are VPC networking and mTLS from inside AgentCore.

**Steps (IAM first, then deploy)**
```bash
git checkout main && git pull                         # cloud versions from main only (§3.3)
scripts/export-agentcore-cert.sh --secret-id temporal/agentcore-worker-mtls   # 4320h; shreds local files
make tf-apply STACK=30-agentcore-iam TF_VAR_create_dev_wci_role=false        # exec role (exists if v0.8 ran); removes dev role
scripts/invoke-role.sh create-or-update --temporal-role-arn $(terraform -chdir=infra/terraform/20-platform output -raw temporal_server_wci_role_arn)
make agentcore-bundle render-config     # VPC mode, subnets, sg-agentcore-worker, endpoint b_<build7>, execution role ARN, envVars (debounce 30)
(cd build/agentcore && agentcore deploy --target default)
aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id $RID --query networkConfiguration
TEMPORAL_PROFILE=eks-pf make register-version CLOUD=1  # create → create-version → set-current-version immediately; try without --allow-no-pollers
TEMPORAL_PROFILE=eks-pf temporal workflow execute -n agents -t support-tq --type PingWorkflow -w ping-1
make wci-history && TEMPORAL_PROFILE=eks-pf make case-a
aws secretsmanager delete-secret --secret-id temporal-kind/agentcore-worker-mtls --recovery-window-in-days 7   # if created in v0.9
```

**Acceptance**
- The WCI history shows AssumeRole (External ID enforced), `GetAgentRuntimeEndpoint` and `InvokeAgentRuntime`.
- `create-version` and `set-current-version` succeed, and the flags used are recorded.
- `ping-1`, then Case A, complete with **zero** other pollers.
- AgentCore logs show mTLS to the Route 53 name, then poll, work, drain after 30 s, and `complete_async_task`.
- Flow logs show the AgentCore ENI connecting to NLB:7233.
- The session count returns to 0.
- The Build ID in the story doc equals `build-id.sh` at the tag.
- The §5 "IAM" and "AgentCore and the Worker" checklists are complete.

**Verify items**
- **Q13, definitive.** Fallback: always pass `--allow-no-pollers`.
- Q6 on 1.32.1.
- 5.4: does the CLI produce a VPC runtime with a named endpoint? Fallback: `create-agent-runtime` and `create-agent-runtime-endpoint` through `bedrock-agentcore-control`.
- Does the CLI accept an existing execution role? Fallback: an inline policy on the CLI-created role.
- AWS Q2: required endpoints and S3 prefix-list egress (start permissive and record).
- Q5 in production form.

**Runbook:** 4.2, 5.3–5.5, Phases 6 and 7, Phase 8 steps 1–5, §6.

**Rollback and cost**
1. `set-current-version` away, or `delete-version`.
2. `agentcore destroy`.
3. `invoke-role.sh delete`.
4. Destroy `30-agentcore-iam`.
5. **ENIs can linger for up to 8 h.** Don't destroy `10-network` the same day.

Sessions cost pennies per run.

**Effort:** 1.5–2 d.

**Talk track:** "Nothing is polling Support's queue. I start a Workflow, Temporal's Worker Controller sees there's no Worker, assumes a narrowly scoped role and invokes AgentCore. A microVM in our VPC dials the frontend over mTLS, does the work, idles and goes away."

### v0.13-scale-to-zero ☁

**Goal.** Case B entirely in the cloud.
1. Finance reaches `input-required` and the session drains.
2. Approval arrives, and the Nexus completion creates a Workflow Task.
3. The WCI makes a **second** `InvokeAgentRuntime`.
4. Session #2 finishes the reply.

Also: the reject variant, `scripts/timeline.py` (one slide merging the Support, Finance and WCI histories), and a small **N=10** load test (`scripts/demo/load.sh`).

**Why here.** This is the payoff of v0.6 and v0.7, and the definitive answer to Q15.

**Steps**
```bash
TEMPORAL_PROFILE=eks-pf scripts/demo/case-b.sh   # waits until task-state = input-required AND logs/WCI show the drain
                                                 # (debounce 30 s + margin) before prompting "approve now?"
temporal workflow describe -n agents -w support-2077            # 1 pending Nexus op
TEMPORAL_PROFILE=eks-pf make approve … && make timeline WF=support-2077
TEMPORAL_PROFILE=eks-pf make load N=10 && scripts/agentcore-sessions.sh   # back to 0
```

**Acceptance**
- From the §5 "End to end" list:
  - Case A uses 1 session, and Case B uses 2.
  - The reply quotes the refund ID.
  - The Agent Card shows in the UI.
  - `issue_refund` is idempotent.
  - The Budgets alert and session alarm exist (since v0.8 or v0.10).
- The reject variant produces an apology.
- `make e2e TEMPORAL_PROFILE=eks-pf` asserts the WCI invocation counts.
- The timeline is saved, redacted.
- The cold start of session #2 is recorded.
- The N=10 load test completes with no WCI failures, and sessions return to 0.
- The runbook's N=50 load test is **deferred to v1.2** and stated as such.

**Verify items**
- **Q15** (confirms or corrects v0.8). Fallbacks:
  - (a) A **nudge**: `approve.sh` also starts a trivial Workflow on `support-tq`, so the WCI is triggered and the backlogged task runs.
  - (b) `AGENTCORE_DEBOUNCE_SECONDS` longer than the approval wait. This loses the second session.
  - Report the result to Temporal in either case (`docs/ask-temporal.md`).
- AWS Q8 session metric names. Promote the alarm from the scripted check if they are confirmed.

**Runbook:** §1.3, Phase 8 steps 6–7. **Effort:** 1 d.

**Talk track:** "Finance is waiting for a human, and Support's compute is gone. Nothing runs and nothing is billed, but the conversation is still there. I approve, and Temporal starts a fresh AgentCore session to finish the reply."

### v1.0.0

**Goal.**
- **`make up`:** `guardrails` → `tf-apply` (10, 20, 30) → `invoke-role` → `eks-temporal` → `finance-deploy` → `nexus-endpoint` → `export-agentcore-cert` → `agentcore-deploy` → `register-version`. Measure how long it takes.
- **`make demo-reset`:** terminates the demo Workflows in both namespaces and resets payments-mock.
- **`make down`,** in Phase 11 order:
  1. `demo-reset`
  2. `set-current-version` away from the serverless version, then `delete-version`
  3. `helm upgrade` with `workercontroller.enabled=false`, so the WCI can't invoke a runtime that's about to disappear
  4. `nexus endpoint delete`
  5. `agentcore destroy` (endpoints, then runtime)
  6. `invoke-role.sh delete`
  7. `helm uninstall`, so the LBC removes the NLB
  8. `kubectl delete ns temporal-workers temporal`
  9. `tf-destroy 30-agentcore-iam`
  10. `tf-destroy 20-platform`: destroys EKS, the IRSA roles, ECR (`force_delete`) and RDS **with a final snapshot** (`skip_final_snapshot=false`, `final_snapshot_identifier=temporal-final-<date>`)
  11. Wait for AgentCore ENIs to clear (poll for up to 8 h)
  12. `tf-destroy 10-network`
  13. Delete the secrets with a recovery window
- **`make down-all`** also deletes RDS snapshots, the External ID secret and `00-guardrails`.
- `docs/demo-script.md`, which includes the Case B drain wait, and a README tag index with Build IDs.

**Acceptance**
- From a fresh clone, `make setup check` is green.
- `scripts/verify/v1.0.sh` runs Cases A and B green in the cloud.
- After `make down`, `aws resourcegroupstaggingapi get-resources --tag-filters Key=project,Values=temporal-customer-support` returns only the final RDS snapshot and the guardrails, allowing for lingering ENIs.
- The ledger has no open item without a recorded fallback.
- scrub-check over the full history passes, as a precondition for making the repo public.

**Talk track:** "From a research doc to two agents in two namespaces on two kinds of compute, talking A2A over Nexus with a human in the loop. It comes up with one command, goes away with another, and has a tag for every step."

### v1.1-versioned-rollout ☁ *(optional, post-1.0)*

**Goal.**
- Change the Supervisor, for example by adding a refund ETA. This gives a new content-hash Build ID and a new endpoint `b_<build7>`, registered from `main`.
- `create-version`, then `set-ramping-version --percentage 50`, then set current.
- A Case B pinned to v1 finishes on v1 while new tickets run on v2.
- Drain v1, then delete its version and endpoint. Run a rollback drill.

**Acceptance**
- The replay test passes.
- The in-flight v1 Workflow completes on v1.
- `describe-version` shows v1 drained before deletion.
- There are at most 10 endpoints.

**Verify items**
- the Phase 10 drainage field
- delete-endpoint flags
- `matching.enableWorkerDeploymentVersionDemotionSignal` (2.12)

**Effort:** 1 d.

**Talk track:** "Every build gets its own immutable AgentCore endpoint, and each conversation stays pinned to the build that started it. I can ship v2 in the middle of a human approval, and the old conversation still finishes on v1."

### v1.2-operate ☁ *(optional, post-1.0)*

**Goal.**
- kube-prometheus-stack with the Temporal dashboards; alert on `tasks_dropped`
- Finance SDK and `temporal_nexus_task_*` metrics
- AgentCore Transaction Search and traces
- the **N=50** load test (runbook Phase 8 step 6)
- a rotation drill: Reloader, and the AgentCore secret re-export with 8 h of overlap

**Acceptance**
- The dashboards show the server, Nexus and both Worker fleets.
- Traces appear.
- All 50 load-test Workflows complete with no WCI failures, and sessions return to 0.
- The rotation drill passes.

**Verify items:** Nexus metric names, 6.3 External ID rotation (documented), and Q12 (optional).

**Effort:** 1 d.

**Talk track:** "Day two: one dashboard for the server, Nexus and both Worker fleets, traces from inside AgentCore, a load test that scales up and back to zero, and certificates that rotate."

---

## 5. Testing strategy by layer

| Layer | What | How | Runs |
|---|---|---|---|
| Contract | Round-trip through the default converter; schema snapshot (optional fields only); Agent Card in sync with its markdown | pytest `unit` + golden JSON | CI |
| Pure logic | Debounce tracker, approval threshold, payments idempotency, config precedence, timeout-budget invariant, `build-id.sh` stability | `unit` (fake clock) | CI |
| Activities | Order and policy lookups, `issue_refund` key reuse | `ActivityEnvironment` | CI |
| Workflows | Finance state machine and timeout; signals and query; Supervisor routing, rejected/failed/`NexusOperationError` handling, nesting, handoff mode | `start_time_skipping()` + ScriptedModel under `"bedrock"` | CI |
| Nexus (2 namespaces) | Routing, operation token, completion, idempotent `a2a-task-<ctx>`, card operation, **end-to-end approval timeout → `failed`** | `start_local(namespace="agents", dev_server_existing_path=<CLI 1.9.1>, extra_args=["--namespace","agent-services"])` + `create_nexus_endpoint` | CI |
| Determinism | Code changes vs recorded live histories | `Replayer` over `tests/histories/*.json` | CI |
| AgentCore entrypoint | Ack, single-start guard, connect timeout, `complete_async_task` in `finally` | Mocked `BedrockAgentCoreApp`; `make agentcore-local` | CI + laptop |
| Packaging | Bundle imports in a clean 3.12 venv; size; Docker builds | `agentcore-bundle-test`, `docker build` | CI |
| Live model | Pinning, no params or forced `toolChoice` on the wire, effort latency, structured output, prompt drift (N=5) | `-m bedrock` + botocore hook | laptop |
| K8s / Helm | Rendered manifests valid; chart keys exist | `kubeconform -strict`; `--dry-run=server` | CI + before install |
| Terraform | fmt, validate, tflint; clean plan after apply | static in CI, plan on laptop | CI + v0.10 |
| Security negatives | No cert or wrong CA rejected; NetworkPolicy blocks; External ID enforced | `scripts/verify/{v0.8,v0.9,v0.11,v0.12}.sh` | at the tag |
| Hygiene | No secrets, account IDs or resource IDs in git | gitleaks + `scrub-check` | pre-commit + CI |
| End to end | Cases A and B, reject, failure, WCI invocation counts, N=10 load | `make e2e TEMPORAL_PROFILE=local\|kind\|eks-pf` | each tag |

CI never holds AWS credentials. `bedrock` and `cloud` tests run only from the laptop.

---

## 6. Verify-item schedule (seed for `docs/verify-ledger.md`)

| Item (runbook ref) | Tag | Fallback |
|---|---|---|
| `load_client_connect_config` name; plugin package and import; envconfig keys (5.3, 5.6) | v0.1 | build connect kwargs by hand; pin to the sample's versions |
| `start_local` dev-server version matches CLI 1.9.1 | v0.1 | `dev_server_existing_path` |
| `structured_output_model` vs forced `tool_choice` on Opus 5.5 (3.2) | v0.2 | unforced capture tool; JSON + pydantic |
| No inference params or forced `toolChoice` on the wire; effort field; model pin (3.2) | v0.2 | — (mandatory) |
| ScriptedModel injectable through `models` | v0.2 | mock the model Activity by name |
| Nested agents as tools (Q20) | v0.3 | sequential sub-agent steps |
| A2A field names vs the spec (§1.4) | v0.4 | rename before v0.11; label as "A2A-shaped" |
| Standalone Nexus ops from the CLI (Q16, 7b.9) | v0.4 (1.32.0), v0.11 (1.32.1) | smoke Workflow on `nexus-smoke-tq` |
| Nexus op summary (Q21); Nexus under time-skipping | v0.4 | log line; `start_local` |
| Standalone-Activity backing (3.4, 7b.4) | **descope proposed: user to confirm (ADR-0004)**; Q16 asked of Temporal | Workflow-backed |
| Async in-workflow `@tool` awaiting Nexus (Q19a) | v0.5 | `SUPPORT_A2A_MODE=handoff` |
| Planned `nexus_operation_as_tool` for Strands (Q19, second half) | ask Temporal alongside the Q15 report (`docs/ask-temporal.md`) | hand-written `@tool` (as built) |
| Prompt drift skips Finance (§6) | v0.5 | guard re-prompt, then handoff |
| No Strands tool timeout on long waits (Q19b) | v0.6 | handoff |
| Caller Nexus timeout vs Finance approval timeout (5.6 vs 7b.4) | v0.6 | 25 h caller budget + explicit `NexusOperationError` handling (ADR-0009) |
| `deployment create` required (7.1) | v0.7 | `create` in the script |
| `--allow-no-pollers` (Q13, 7.2) | v0.7 (informational); **v0.12 definitive** (v0.8 uses a local Worker, so it doesn't answer this) | always pass it |
| Minimum `temporalio` (Q4); CodeZip dependency file; CLI 1.0.x commands | v0.7, v0.8 | ≥ 1.34; Container arm64; control-plane API |
| WCI present in the dev server | v0.7 | skip v0.8 |
| WCI namespace constraint to `agents` (Phase 3) | v0.9 (not expressible on `start-dev`) | — |
| WCI namespace, IDs, activities (Q6); CFN params (6.2); External ID check | v0.8 → v0.12 | — |
| Native secret injection (AWS Q4) | v0.8 | fetch at startup (as designed) |
| AgentCore Terraform resource names (AWS Q6) | v0.8 (schema grep) | AgentCore CLI (ADR-0008) |
| UI shows the compute config (Q9) | v0.8 / v0.13 | CLI `describe-version` |
| **Nexus completion wakes the WCI (Q15)** | **v0.8 (first, guarded, may be inconclusive), v0.13 (definitive)** | nudge Workflow; long debounce; report to Temporal |
| Chart keys, admintools mounts, frontend labels (2.1, 2.2, 7b.6, Q8) | v0.9 | adjust values; manual PDBs; patch the schema job |
| WCI startup logs; dynamic config reload without restart | v0.9 | rolling restart |
| arm64 images | v0.9 | m6i.xlarge + amd64 builds |
| Server cert hot-reload (Q10); SDK file-cert reload (Q18) | v0.9 | Reloader / rolling restart |
| `TEMPORAL_TLS_*` names; internode EKUs (2.5) | v0.9 | explicit `TLSConfig` |
| mTLS from Secrets Manager (Q5) | v0.9 (optional), v0.12 | PEM temp files + `_PATH` |
| kindnet NetworkPolicy | v0.9 | Calico |
| Module majors; VPC CNI netpol; `force_ssl`; STS endpoint (1.1–1.4) | v0.10 | pin the current major; keep the endpoint |
| CDK bootstrap; `CreateServiceLinkedRole` and `PassRole` (3.2); RDS minor 16.6 (2.x) | v0.1 doctor + v0.10 | bootstrap CDK; grant permissions; record the chosen minor |
| RDS TLS; schema job CA (2.2, 2.3) | v0.11 | `useHelmHooks: false` + manual schema |
| NLB passthrough (2.4) | v0.11 | — (NLB is decided) |
| Global-profile IAM / SCP (3.2) | v0.11 (IRSA), v0.12 (execution role) | `us.` profile via a variable |
| CLI VPC runtime + named endpoint (5.4); existing execution role | v0.12 | control-plane API; inline policy |
| Required endpoints, S3 prefix-list egress (AWS Q2, 1.5) | v0.12 | keep everything; add an SG rule |
| CloudWatch session metrics (AWS Q8) | v0.8 (scripted check), v0.13 (alarm) | Budgets + `agentcore-sessions.sh` |
| `agentcore destroy` name (Phase 11) | v0.8 | control-plane deletes |
| Drainage field, delete-endpoint flags, demotion signal (Phase 10, 2.12) | v1.1 | — |
| Nexus metric names (Phase 9); External ID rotation (6.3) | v1.2 | documented only |
| **Out of scope** (recorded as "not exercised" in ADRs): Pod Identity `TagSession` (Q3); hidden dynamic-config keys (Q7); Aurora/OpenSearch (Q11); principal attribution (Q12, optional in v1.2); XDC (Q14); option (b) ClaimMapper with Nexus (Q17); Code Interpreter IAM (AWS Q5); pricing (AWS Q7) | — | IRSA, single cluster, PostgreSQL and option (c) are decided |

---

## 7. Dependency graph and critical path

```
v0.0 ─► v0.1 ─► v0.2 ─► v0.3 ─► v0.4 ─► v0.5 ─► v0.6 ─► v0.7 ─┬─► v0.8 (optional, cents) ─┐
                                  │                            │                          │
                                  │                            └─► v0.9 kind+mTLS ─(G2,G3)┤
                                  │                                                       ▼
                                  └── Terraform authored on a branch (no apply) ──► v0.10 ☁ ─► v0.11 ☁ (G4) ─► v0.12 ☁ ─► v0.13 ☁ ─► v1.0.0
                                                                                                                                  └─► v1.1, v1.2 (optional)
```

- Tags are linear on `main`, in presentation order.
- v0.8 and v0.9 don't depend on each other. If v0.8 is skipped, `make guardrails external-id cfn-template` run at the first AWS touch (v0.9 optional SM path, or v0.10).
- **Critical path:**
  1. v0.2 and v0.5 (plugin + Nexus: Q19, Q20, structured output)
  2. v0.7 (Worker lifecycle and packaging)
  3. v0.9 (the full values and certificate set)
  4. v0.11, then **v0.12** (WCI + AgentCore VPC + IAM, the highest-risk cloud tag)
  5. v0.13 (Q15)

**Hard gates**

| Gate | Condition | If it fails |
|---|---|---|
| G1 (v0.6) | Q19a or Q19b fails | switch the default to handoff before v0.7 |
| G2 (end of v0.9, written decision) | Q15 status from v0.8 (or unknown) | choose and pre-wire the fallback (nudge or long debounce); the user decides whether to start AWS spend |
| G3 (v0.9) | Case B over mTLS on kind passes | no `tf-apply` until it does |
| G4 (v0.11) | hybrid Case B on EKS passes | no AgentCore VPC deploy until it does |
| G5 (v0.8, optional SM path in v0.9, every ☁ tag) | — | explicit go-ahead from the user before provisioning anything in AWS |

**If time is short:**
- Fold v0.3 into v0.2.
- Skip v0.8.
- Drop v1.1 and v1.2.
- Minimum compelling cut: v0.2, v0.5, v0.6, v0.7, v0.9, v0.11, v0.12, v0.13.

## 8. Cost and rollback summary

| Tag | Creates | Standing cost (≈, ⚠️ check pricing) | Rollback |
|---|---|---|---|
| v0.1–v0.7 | nothing in AWS | Bedrock tokens only | — |
| v0.8 | Budgets alert + session alarm (permanent), External ID secret, IAM roles, CFN role stack, PUBLIC runtime | cents | same day: `delete-version`, `agentcore-down`, `invoke-role.sh delete`, session check = 0 |
| v0.9 | optional secret `temporal-kind/agentcore-worker-mtls` | ~$0.40/mo | delete at v0.12 |
| v0.10 | VPC, NAT (1 EIP), endpoints, EKS, nodes, RDS, ECR | ~$0.60/h (~$15/day) | `make pause`; `tf-destroy` in reverse order |
| v0.11 | Helm release, NLB, Route 53 record, Finance, Nexus endpoint | +~$0.02/h | `helm uninstall`; RDS snapshot; delete endpoint and namespace |
| v0.12–v0.13 | AgentCore VPC runtime and endpoint, CFN stack, mTLS secret | per-second sessions + Opus tokens | Phase 11 order; **ENIs can linger up to 8 h** before `10-network` |
| v1.1–v1.2 | extra endpoint, monitoring stack | small | delete the old endpoint; uninstall monitoring |

**Account notes**
- us-east-1 is at 4/5 VPCs and 8/10 EIPs. This project takes the last VPC slot and one EIP, and `make check-quotas` re-checks before applying.
- Keep a single NAT gateway.
- Expected spend from v0.10 to v1.0 is about $100–150 including Bedrock, with overnight pauses.
- Use `make down` between presentations that are weeks apart. `make up` rebuilds in about 30–40 minutes.

---

## Appendix: Review dispositions

Every high and medium issue was applied. These lines record the parts that were rejected or changed, and why:

- **CFN invocation-role stack via Terraform `aws_cloudformation_stack` with the External ID as a data source.** Rejected. It would put the External ID in Terraform state, which conflicts with ADR-0007 and runbook 6.3. The stack is managed by `scripts/invoke-role.sh` instead. The ordering part of that issue (IAM before `agentcore deploy`, and the same tooling in v0.8 and v0.12) was accepted.
- **Use the SSO permission-set role ARN as `TemporalIamRoleArn` in v0.8.** Rejected in favour of the dedicated `temporal-dev-wci` role, per the separate SSO-trust issue. That role is stable across permission-set re-provisioning and mirrors `temporal-server-wci`.
- **Dynamic-config YAML for the namespace constraint on the dev server.** Not possible. `temporal server start-dev --help` (CLI 1.9.1) offers only `--dynamic-config-value KEY=VALUE`, so the difference is documented and the constraint is verified on kind in v0.9.
- **Fold v0.11 aws-foundation into the EKS tag.** Kept as its own tag (`v0.10-aws-foundation`). AZ-ID subnet selection, the endpoints and the guardrails are a deliberate talking point. The main line still has 15 tags.
- **Synthesis self-contradiction.** Synthesis notes are omitted from this final plan as instructed. The two runbook deviations they described (the PUBLIC probe runtime, and m7g.large vs m6i.xlarge) are recorded in ADR-0005 and ADR-0006, with the m6i.xlarge fallback kept.
