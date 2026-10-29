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

## Quick start (laptop, no AWS needed except `bedrock-ping`)

```bash
make setup          # uv python install 3.12; uv sync --all-packages; .env from .env.example
make doctor         # tool versions + AWS identity (DOCTOR_SKIP_AWS=1 to skip AWS)
make check          # lint, types, unit, workflow, integration, scrub-check (no AWS)
make dev-server     # Temporal dev server with namespaces agents + agent-services, UI :8233
make bedrock-ping   # Opus 5.5 answers via global. and us. profiles (a few tokens)
```

`make help` lists every target. Use `127.0.0.1`, not `localhost`.

## Story index

Each tag adds one concept. `docs/story/<tag>.md` says what it adds, how to run it,
what to look at and which unknowns it closed.

| Tag | Adds | Run it | Talk track | Build ID |
|---|---|---|---|---|
| [`v0.0-research`](TEMPORAL_SELFHOSTED_AGENTCORE_RUNBOOK.md) | The design and its unknowns | — | "Before any code we wrote down the design and the twenty-odd things we didn't know." | — |
| [`v0.1-scaffold`](docs/story/v0.1-scaffold.md) | A repo you can trust: uv workspace, two-namespace dev server, `make check`, Opus 5.5 ping | `make check dev-server bedrock-ping` | "One command gives you a two-namespace Temporal, and `make check` runs real Temporal tests with no cloud access." | — |
| [`v0.2-durable-agent`](docs/story/v0.2-durable-agent.md) | Support Supervisor `TemporalAgent` with Activity tools; survives `kill -9` mid-thought | `make dev-server worker-support case-a` (offline: `worker-support-scripted`) | "Every model call and tool call is an Activity. I kill the process mid-thought, and it carries on from exactly where it stopped." | — |
| [`v0.3-sub-agents`](docs/story/v0.3-sub-agents.md) | Order Lookup and Return Policy sub-agents as in-workflow tools; three agents attributed in one Workflow | `make dev-server worker-support case-a case-b sub-agents` (offline: `worker-support-scripted`, `spike-nested`) | "Support now has its own small team of agents, used as tools, in-process. Keep that in mind, because the next agent belongs to someone else." | — |
| [`v0.4-finance-agent`](docs/story/v0.4-finance-agent.md) | A2A-shaped contract; Finance Refund agent in `agent-services` behind Nexus endpoint `finance-agent`; no-LLM smoke caller | `make dev-server-up payments-mock worker-finance-scripted worker-smoke nexus-endpoint smoke card task-state` (or `make dev`) | "The Finance team publishes exactly one thing, an A2A-shaped contract. No AI on the caller yet, just the hop. A Workflow in `agents` calls a named endpoint, and the server routes it into another namespace. Workers never talk to each other." | — |
| [`v0.5-a2a-over-nexus`](docs/story/v0.5-a2a-over-nexus.md) | The Supervisor discovers Finance's Agent Card and asks Finance itself: `ask_finance_agent`, an async in-workflow `@tool` awaiting `send_message` over Nexus (Q19a); `SUPPORT_A2A_MODE=handoff` fallback; prompt-drift guard | `make dev` then `make nexus-endpoint case-a timeline task-state` (offline: `worker-support-scripted worker-finance-scripted`; `SUPPORT_A2A_MODE=handoff make case-a`) | "Support can't move money, so it asks Finance: a different team's agent, through a published contract and a named endpoint. It isn't a child workflow, and only the contract is shared." | — |

## Layout

```
packages/finance-a2a-api   the Finance agent's published contract (only shared code)
packages/demo-testing      test-only: two-namespace dev server, ScriptedModel, history helpers
services/support-agent     team Support -> namespace agents
services/finance-agent     team Finance -> namespace agent-services
services/payments-mock     idempotent refund API (v0.4, :8081)
config/temporal.toml       envconfig profiles (TEMPORAL_PROFILE=local)
scripts/                   doctor, dev-server, bedrock_ping, scrub-check, redact, demo/, verify/<tag>.sh
spikes/                    one file per verify item (Opus 5.5 params, structured output)
```
