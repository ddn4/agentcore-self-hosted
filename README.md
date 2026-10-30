# Self-hosted Temporal + AgentCore: customer-support A2A demo

A customer-support **Support Supervisor** agent (namespace `agents`, eventually a
serverless Worker on Amazon Bedrock AgentCore) asks a **Finance Refund** agent
owned by another team (namespace `agent-services`, on EKS) for a refund, over a
published A2A-shaped contract carried by **Temporal Nexus**. Both agents are
Strands agents made durable by the Temporal Strands plugin, running on Claude
Opus 5.5 on Bedrock, on a **self-hosted** Temporal 1.32 server.

- Design: [`TEMPORAL_SELFHOSTED_AGENTCORE_RUNBOOK.md`](TEMPORAL_SELFHOSTED_AGENTCORE_RUNBOOK.md)
- Plan (tags, conventions, testing): [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md)
- Decisions: [`docs/adr/`](docs/adr/README.md) · Unknowns: [`docs/verify-ledger.md`](docs/verify-ledger.md) · Versions: [`docs/versions.md`](docs/versions.md)

## Run the demo on your laptop

Everything below runs on one laptop. Each scenario has a **live** form (Opus 5.5 on Bedrock)
and an **offline** form (scripted models, no AWS); the Temporal side is identical. Run each
process in its own terminal (or append `&`), and stop everything when you are done.

### Prerequisites

| Need | For | Check |
|---|---|---|
| Temporal CLI 1.9.x (dev server 1.32), uv >= 0.12, `jq`, `curl`, `git` | all scenarios | `make doctor` |
| Ports 7233, 8233 (Temporal, UI), 8080 (AgentCore app), 8081 (payments-mock) free | all scenarios | `lsof -iTCP -sTCP:LISTEN -nP` |
| Docker with context `colima-agentcore` (6 CPU / 12 GB; the global context is never changed), kind, helm v4, kubectl | scenario 4 (kind + mTLS) | `make doctor` |
| AWS default profile (SSO) with Bedrock access to `global.anthropic.claude-opus-5-5` in `us-east-1` | live runs only | `aws sso login`, then `make bedrock-ping` |

No AWS resource is created by any laptop scenario. Use `127.0.0.1`, not `localhost`.

```bash
make setup          # uv python install 3.12; uv sync --all-packages; .env from .env.example
make doctor         # tool versions + AWS identity (DOCTOR_SKIP_AWS=1 to skip AWS)
make check          # lint, types, unit, workflow, integration, infra-static, scrub-check (no AWS)
make bedrock-ping   # live only: Opus 5.5 answers via global. and us. profiles (a few tokens)
eval "$(make -s env)"   # only for bare `temporal ...` commands: points the CLI at config/temporal.toml
```

`make` targets set `TEMPORAL_CONFIG_FILE`/`TEMPORAL_PROFILE` themselves. A bare `temporal`
command in your shell does not, and would read your own default config file (macOS: `~/Library/Application Support/temporalio/temporal.toml`)
(possibly a Temporal Cloud profile), hence `eval "$(make -s env)"`.

### Scenario 1: dev server, Case A ($40, auto-approved by Finance)

```bash
make dev-server-up                      # namespaces agents + agent-services, UI http://127.0.0.1:8233
make payments-mock                      # :8081
make worker-finance                     # offline: make worker-finance-scripted
make worker-support                     # offline: make worker-support-scripted
make nexus-endpoint                     # endpoint finance-agent -> agent-services (idempotent)
make case-a                             # support-1042: Supervisor -> sub-agents -> Finance over Nexus
make timeline WF=support-1042           # both namespaces; the A2A hop is the NEXUS line
make task-state WF=support-1042         # Finance's A2A task: completed, approved_by auto
```

Live shortcut: `make dev` runs the dev server, payments-mock, both real Workers and the
endpoint under honcho (Ctrl-C stops all).
Story: [v0.2](docs/story/v0.2-durable-agent.md) to [v0.5](docs/story/v0.5-a2a-over-nexus.md).

### Scenario 2: dev server, Case B ($450, input-required, a human approves from the CLI)

Same processes as scenario 1, then:

```bash
make case-b                             # starts support-2077 and returns
make task-state WF=support-2077         # repeat until "input-required" (~30 s)
make pending WF=support-2077            # Support: one Nexus send_message STARTED, budget 90000s
# stop the Support Worker (Ctrl-C): nothing is lost while Finance waits
make approve WF=support-2077 APPROVER=dana.finance COMMENT="photos checked"
make worker-support                     # (or -scripted) start it again
make result WF=support-2077             # the reply quotes Finance's refund ID
make approval-report WF=support-2077    # how long Support waited, who approved
```

Reject instead: `make reject WF=... APPROVER=dana.finance`. Story:
[v0.6](docs/story/v0.6-human-in-the-loop.md).

### Scenario 3: the Support Worker in serverless (AgentCore) shape

Keep the dev server, payments-mock, a Finance Worker and the endpoint from scenario 1; stop
the plain Support Worker (the serverless one uses the versioned queue `support-tq`).

```bash
make agentcore-local                    # offline: make agentcore-local-scripted (:8080)
make invoke-local                       # "I am the WCI": lifetime 1 starts polling
make register-version MODE=--poller     # support-supervisor:<build id> becomes current (PINNED)
make case-b-serverless WF=support-2077-sl   # Finance goes input-required
make agentcore-ping                     # Healthy again ~20 s later: the Worker drained
make approve WF=support-2077-sl APPROVER=dana.finance
make invoke-local                       # lifetime 2 finishes the conversation
make result WF=support-2077-sl
uv run --frozen python scripts/serverless_report.py support-2077-sl   # 2 lifetimes, 1 Build ID
```

With the dev server's own Worker Controller Instance doing the invoking (local AgentCore
API emulator, nothing reaches AWS): wait until `make agentcore-ping` says `Healthy`, then
`make dev-server-down; make dev-server-up WCI=1`, `make agentcore-emulator`,
`make register-version`, `make case-b-serverless WF=support-2077-wci`, approve, and
`make wci-history` (one `InvokeWorker` per invocation). The Build ID comes from
`scripts/build-id.sh` and refuses uncommitted Support changes. Story:
[v0.7](docs/story/v0.7-serverless-shape.md).

### Scenario 4: the real server on kind, with mTLS everywhere

```bash
make dev-server-down                    # 127.0.0.1:7233 must be free
make kind-up kind-temporal              # kind + cert-manager CA + postgres + chart 1.7.0 (server 1.32.1), mTLS
make kind-workers FINANCE_MODEL=scripted    # live: make kind-aws-creds kind-workers FINANCE_MODEL=bedrock
make certs-pull pf                      # client certs -> .certs/, port-forwards :7233 and UI :8233
export TEMPORAL_PROFILE=kind            # every make target below now dials kind over mTLS
make nexus-endpoint
make agentcore-local-scripted           # live: make agentcore-local (agentcore-worker client cert)
make invoke-local register-version MODE=--poller
make case-a-serverless WF=support-1042-sl
make task-state WF=support-1042-sl      # handled_by: a Finance pod
make case-b-serverless WF=support-2077-sl   # then approve as in scenario 3
make kind-down                          # deletes the cluster and stops the port-forwards
```

Story: [v0.9](docs/story/v0.9-temporal-on-kind-mtls.md).

### Clean up

Stop the Workers and apps (Ctrl-C), then `make dev-server-down` (and `make dev-server-reset`
to forget all Workflows) or `make kind-down`. Nothing else keeps running.

## Story index

Each tag adds one concept: tag -> what it adds -> its story doc (`docs/story/<tag>.md`: how
to run it, what to look at, which unknowns it closed). What is still open for the AWS tags
(v0.8, v0.10+) is summarised at the top of [`docs/verify-ledger.md`](docs/verify-ledger.md#still-open-for-the-aws-tags).

| Tag | Adds | Run it | Talk track | Build ID |
|---|---|---|---|---|
| [`v0.0-research`](TEMPORAL_SELFHOSTED_AGENTCORE_RUNBOOK.md) | The design and its unknowns | — | "Before any code we wrote down the design and the twenty-odd things we didn't know." | — |
| [`v0.1-scaffold`](docs/story/v0.1-scaffold.md) | A repo you can trust: uv workspace, two-namespace dev server, `make check`, Opus 5.5 ping | `make check dev-server bedrock-ping` | "One command gives you a two-namespace Temporal, and `make check` runs real Temporal tests with no cloud access." | — |
| [`v0.2-durable-agent`](docs/story/v0.2-durable-agent.md) | Support Supervisor `TemporalAgent` with Activity tools; survives `kill -9` mid-thought | `make dev-server worker-support case-a` (offline: `worker-support-scripted`) | "Every model call and tool call is an Activity. I kill the process mid-thought, and it carries on from exactly where it stopped." | — |
| [`v0.3-sub-agents`](docs/story/v0.3-sub-agents.md) | Order Lookup and Return Policy sub-agents as in-workflow tools; three agents attributed in one Workflow | `make dev-server worker-support case-a case-b sub-agents` (offline: `worker-support-scripted`, `spike-nested`) | "Support now has its own small team of agents, used as tools, in-process. Keep that in mind, because the next agent belongs to someone else." | — |
| [`v0.4-finance-agent`](docs/story/v0.4-finance-agent.md) | A2A-shaped contract; Finance Refund agent in `agent-services` behind Nexus endpoint `finance-agent`; no-LLM smoke caller | `make dev-server-up payments-mock worker-finance-scripted worker-smoke nexus-endpoint smoke card task-state` (or `make dev`) | "The Finance team publishes exactly one thing, an A2A-shaped contract. No AI on the caller yet, just the hop. A Workflow in `agents` calls a named endpoint, and the server routes it into another namespace. Workers never talk to each other." | — |
| [`v0.5-a2a-over-nexus`](docs/story/v0.5-a2a-over-nexus.md) | The Supervisor discovers Finance's Agent Card and asks Finance itself: `ask_finance_agent`, an async in-workflow `@tool` awaiting `send_message` over Nexus (Q19a); `SUPPORT_A2A_MODE=handoff` fallback; prompt-drift guard | `make dev` then `make nexus-endpoint case-a timeline task-state` (offline: `worker-support-scripted worker-finance-scripted`; `SUPPORT_A2A_MODE=handoff make case-a`) | "Support can't move money, so it asks Finance: a different team's agent, through a published contract and a named endpoint. It isn't a child workflow, and only the contract is shared." | — |
| [`v0.6-human-in-the-loop`](docs/story/v0.6-human-in-the-loop.md) | Finance's human approver: over $100 or HIGH risk the A2A task goes `input-required` and waits (up to 24 h) for `approve`/`reject` signals; no decision → `failed`. Support budgets 25 h (ADR-0009) and apologises or escalates on `rejected`/`failed`; Support can be stopped while Finance waits | `make case-b task-state pending`, stop the Support worker, `make approve WF=support-2077 APPROVER=dana.finance`, start it again, `make result approval-report` | "Over $100, Finance's agent says 'I need a human', and the task goes to input-required. I switch Support off completely. Nothing is lost, and when the approver signals, the conversation picks up again." | — |
| [`v0.7-serverless-shape`](docs/story/v0.7-serverless-shape.md) | Support Worker in AgentCore shape: `BedrockAgentCoreApp` entrypoint, debounce/drain/`complete_async_task`, PINNED Worker Versioning on `support-tq`, content-hash Build ID, CodeZip bundle; the dev server's own WCI invokes it through a local AgentCore API emulator | `make agentcore-local-scripted invoke-local register-version MODE=--poller case-b-serverless`; with the WCI: `make dev-server-up WCI=1 agentcore-emulator register-version`; `make agentcore-bundle agentcore-bundle-test` | "Support's Worker is now built to die. It starts when invoked, drains when idle, and when Finance answers, a new invocation picks the conversation up, pinned to the exact build." | `32b04fb` (local) |
| `v0.8-wci-handshake` | *Skipped in this run* (needs AWS resources and go-ahead G5); its items move to v0.12/v0.13 | — | — | — |
| [`v0.9-temporal-on-kind-mtls`](docs/story/v0.9-temporal-on-kind-mtls.md) | The real server on the laptop: chart temporal-1.7.0 / server 1.32.1 on kind (postgres, WCI constrained to `agents`), Finance + payments-mock as pods; then mTLS everywhere from a free cert-manager CA (one CN per identity), NetworkPolicy, rotation | `make kind-up kind-temporal kind-workers certs-pull pf`, `TEMPORAL_PROFILE=kind make nexus-endpoint agentcore-local-scripted invoke-local register-version MODE=--poller case-b-serverless approve`; `make kind-down` | "Same code, real server, locked down: one free CA, one identity per Worker, wrong certs refused, and the A2A contract still hasn't changed." | `32b04fb` (unchanged) |
| [`v0.9.1-laptop-complete`](docs/story/v0.9.1-laptop-complete.md) | Acceptance on a clean slate: every laptop scenario above run from this README alone; doc and script gaps fixed (`make env`) | the four scenarios above | "Everything up to here runs on one laptop, from a clean checkout, with one README." | `32b04fb` (unchanged) |
| [`v0.9.2-live-bedrock`](docs/story/v0.9.2-live-bedrock.md) | Every laptop scenario on Opus 5.5, live: spikes, Case A/B, the WCI-invoked AgentCore app, kind + mTLS with Finance on Bedrock, `make test-live`; three stale checks fixed | `aws sso login`, then the live variants above and `make spikes test-live` | "Same demo, real model, every path." | `0015bea` (tests only) |

## Layout

```
packages/finance-a2a-api   the Finance agent's published contract (only shared code)
packages/demo-testing      test-only: two-namespace dev server, ScriptedModel, history helpers
services/support-agent     team Support -> namespace agents
services/finance-agent     team Finance -> namespace agent-services
services/payments-mock     idempotent refund API (v0.4, :8081)
config/temporal.toml       envconfig profiles (TEMPORAL_PROFILE=local | kind | kind-plaintext)
deploy/                    kind cluster, Helm values (chart 1.7.0), cert-manager + k8s manifests (v0.9)
scripts/                   doctor, dev-server, bedrock_ping, scrub-check, redact, demo/, verify/<tag>.sh
spikes/                    one file per verify item (Opus 5.5 params, structured output)
```
