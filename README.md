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

## Layout

```
packages/finance-a2a-api   the Finance agent's published contract (only shared code)
packages/demo-testing      test fixtures (two-namespace dev server; later ScriptedModel)
services/support-agent     team Support -> namespace agents
services/finance-agent     team Finance -> namespace agent-services
services/payments-mock     idempotent refund API (v0.4)
config/temporal.toml       envconfig profiles (TEMPORAL_PROFILE=local)
scripts/                   doctor, dev-server, bedrock_ping, scrub-check, redact, verify/<tag>.sh
```
