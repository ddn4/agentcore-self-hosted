# Versions actually tested

Pre-release software (Serverless Workers, the Strands plugin) is pinned to what
was tested. `make doctor` reads the **Temporal CLI** row; `tests/test_harness.py`
asserts that it matches `demo_testing.TEMPORAL_CLI_VERSION` and the installed CLI.
Python packages are locked in `uv.lock`; the table records the resolved versions.

## Tools

| Tool | Version | Since | Notes |
|---|---|---|---|
| Temporal CLI | 1.9.1 | v0.1 | embeds server 1.32.0 and UI 2.54.1; `make dev-server`, `start_local(dev_server_existing_path=...)` |
| Temporal dev server | 1.32.0 | v0.1 | from the CLI; the Helm/EKS server is 1.32.1 (v0.9+) |
| Temporal server image | 1.32.1 | v0.9 | planned |
| Temporal Helm chart | 1.7.0 | v0.9 | planned |
| uv | 0.12.15 | v0.1 | |
| Python | 3.12.14 | v0.1 | uv-managed; matches AgentCore `PYTHON_3_12` |
| Node.js | 24.16.0 | v0.1 | >= 20.12 required for `@aws/agentcore` (v0.8) |
| AWS CLI | 2.36.46 | v0.1 | |
| kind | 0.33.0 | v0.1 (doctor) | used from v0.9 |
| Helm | 4.3.1 | v0.1 (doctor) | used from v0.9 |
| kubectl | 1.37.0 | v0.1 (doctor) | used from v0.9 |
| colima | 0.10.3 | v0.1 (doctor) | docker context `colima-agentcore` (6 CPU / 12 GB) |
| gitleaks | not installed | — | `scrub-check` falls back to grep locally; CI runs gitleaks |
| Terraform | not installed | — | >= 1.10, from v0.10 |
| `@aws/agentcore` CLI | not installed | — | 1.0.x, from v0.8 |

## Python packages (uv.lock at v0.1)

| Package | Version | Constraint | Notes |
|---|---|---|---|
| temporalio | 1.35.0 | `>=1.34.0,<2` | 1.34.0 is the plugin's minimum |
| temporalio-strands-agents | 0.1.0 | `>=0.1.0` | import path `temporalio.strands_agents` |
| strands-agents | 1.62.0 | `>=1.39.0` | |
| nexus-rpc | 1.4.0 | `>=1.1.0,<2` | the contract (`finance_a2a_api`, v0.4) and Finance |
| bedrock-agentcore | 1.25.1 | `>=1.9.1` | Support only (AgentCore entrypoint, v0.7) |
| boto3 / botocore | 1.43.122 | `>=1.34.92,<2` | |
| httpx | 0.28.1 | `>=0.27` | Finance → payments-mock (v0.4) |
| pydantic | 2.14.2 | (transitive) | the plugin installs the pydantic data converter |
| ruff | 0.17.3 | dev | |
| pyright | 1.1.414 | dev | |
| pytest / pytest-asyncio | 9.2.0 / 1.4.1 | dev | |
| honcho | 2.0.0 | dev | `make dev` |

## Model

| Item | Value |
|---|---|
| Model | Claude Opus 5.5 on Bedrock |
| Default profile | `global.anthropic.claude-opus-5-5` (`BEDROCK_MODEL_ID`) |
| US-only profile | `us.anthropic.claude-opus-5-5` (same code; IAM differs) |
| Region | `us-east-1` (`AWS_REGION`) |
| Effort | `BEDROCK_EFFORT` (`low`…`max`), default `medium` = model default, not sent (ADR-0001) |

## Effort latency (v0.2)

Per Supervisor turn (Case A first turn, tools attached), through the plugin's
`invoke_model` Activity: `spikes/opus_params.py --turns 3`.

| Effort | Latency per turn (s) | Output tokens | Measured |
|---|---|---|---|
| low | pending | pending | live run pending (AWS SSO session expired at v0.2) |
| medium | pending | pending | live run pending |

## Protocols (v0.4)

| Protocol | Version | Notes |
|---|---|---|
| A2A (shape only) | 0.3.0 | `finance_a2a_api` follows A2A 0.3.0 concepts in snake_case; A2A 1.0.0 deltas in `packages/finance-a2a-api/README.md` |
| Nexus on the dev server | CLI 1.9.1 / server 1.32.0 | Standalone Nexus Operations need `--dynamic-config-value nexusoperation.enableStandalone=true` (set by `scripts/dev-server.sh`) |
