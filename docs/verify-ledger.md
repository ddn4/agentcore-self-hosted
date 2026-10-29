# Verify ledger

Every ⚠️ item in the runbook and every open question in runbook §7, with the tag
that answers it, the result and the fallback taken. Seeded from
IMPLEMENTATION_PLAN.md §6. Status: **open**, **resolved**, **fallback** (answered,
fallback taken), **descoped**, **out of scope**, **ask Temporal**.

| # | Item (runbook ref) | Tag | Status | Result / evidence | Fallback |
|---|---|---|---|---|---|
| 1 | `ClientConfig.load_client_connect_config()` name (5.3) | v0.1 | **resolved** | Exists in temporalio 1.35.0 as a static method: `load_client_connect_config(profile=None, *, config_file=None, disable_file=False, disable_env=False, config_file_strict=False, override_env_vars=None) -> ClientConnectConfig` (`temporalio/envconfig.py`). Used by `support_agent.config.connect_config()` and `finance_agent.config.connect_config()`; exercised by `scripts/hello_smoke.py` (story v0.1). | — |
| 2 | Strands plugin package and import path (5.6) | v0.1 | **resolved** | Package `temporalio-strands-agents` 0.1.0 (needs `temporalio>=1.34.0`; resolved 1.35.0). Import `from temporalio.strands_agents import StrandsPlugin, TemporalAgent`; workflow helpers in `temporalio.strands_agents.workflow`. `temporalio.contrib.strands` is now only a compatibility shim. `StrandsPlugin(models=..., mcp_clients=..., sandboxes=..., ...)`; with no `models` it registers Strands' default `BedrockModel()` under `"bedrock"`. | — |
| 3 | envconfig TOML keys and `TEMPORAL_CONFIG_FILE` | v0.1 | **resolved** | Keys `profile.<name>.address`, `.namespace`, `.api_key`, `.tls.disabled`, `.tls.server_name`, `.tls.client_cert_path|_data`, `.tls.client_key_path|_data`, `.tls.server_ca_cert_path|_data`, `.grpc_meta` (docs: references/client-environment-configuration). Env `TEMPORAL_CONFIG_FILE` and `TEMPORAL_PROFILE` select file and profile for **both** the SDK and CLI 1.9.1; env vars override the file. `[profile.local.tls] disabled = true` is enough for the CLI (no `TEMPORAL_TLS` needed). Tests: `services/*/tests/test_config.py`; CLI: verify step 4. | — |
| 4 | `start_local` dev-server version matches CLI 1.9.1 | v0.1 | **resolved** | `WorkflowEnvironment.start_local(namespace="agents", dev_server_existing_path=shutil.which("temporal"), dev_server_extra_args=["--namespace","agent-services"])` starts the installed CLI with both namespaces. `tests/test_harness.py` asserts both namespaces, server 1.32.x and CLI == `docs/versions.md`. Download fallback string format is the release tag, `"v1.9.1"` (as in samples-python `tests/conftest.py`). | `dev_server_download_version="v1.9.1"` when no CLI on PATH |
| 5 | Tool versions recorded | v0.1 | **resolved** | `docs/versions.md`; `make doctor`. | — |
| 6 | Opus 5.5 answers on both `global.` and `us.` profiles with no inference params | v0.1 | **resolved** | `make bedrock-ping`: both `end_turn`, wire request keys `['messages']` only (story v0.1). | — |
| 7 | `structured_output_model` vs forced `tool_choice` on Opus 5.5 (3.2) | v0.2 | open | | unforced capture tool; JSON + pydantic |
| 8 | No inference params or forced `toolChoice` on the wire through Strands; effort field; model pin (3.2) | v0.2 | open (pin + raw Converse done in v0.1) | | — (mandatory) |
| 9 | ScriptedModel injectable through `models` | v0.2 | open | | mock the model Activity by name |
| 10 | Nested agents as tools (Q20) | v0.3 | open | | sequential sub-agent steps |
| 11 | A2A field names vs the spec (§1.4) | v0.4 | open | | rename before v0.11; label "A2A-shaped" |
| 12 | Standalone Nexus ops from the CLI (Q16, 7b.9) | v0.4 (1.32.0), v0.11 (1.32.1) | open | | smoke Workflow on `nexus-smoke-tq` |
| 13 | Nexus op summary (Q21); Nexus under time-skipping | v0.4 | open | | log line; `start_local` |
| 14 | Standalone-Activity Nexus backing (3.4, 7b.4) | — | **descoped** | User confirmed the descope (ADR-0004). Workflow-backed only. Q16 still asked of Temporal (`docs/ask-temporal.md`). | Workflow-backed |
| 15 | Async in-workflow `@tool` awaiting Nexus (Q19a) | v0.5 | open | | `SUPPORT_A2A_MODE=handoff` |
| 16 | Planned `nexus_operation_as_tool` for Strands (Q19, second half) | ask Temporal | **ask Temporal** | `docs/ask-temporal.md` | hand-written `@tool` |
| 17 | Prompt drift skips Finance (§6) | v0.5 | open | | guard re-prompt, then handoff |
| 18 | No Strands tool timeout on long waits (Q19b) | v0.6 | open | | handoff |
| 19 | Caller Nexus timeout vs Finance approval timeout (5.6 vs 7b.4) | v0.6 | open | | 25 h caller budget + explicit `NexusOperationError` handling (ADR-0009) |
| 20 | `deployment create` required (7.1) | v0.7 | open | | `create` in the script |
| 21 | `--allow-no-pollers` (Q13, 7.2) | v0.7 (info), v0.12 (definitive) | open | | always pass it |
| 22 | Minimum `temporalio` (Q4); CodeZip dependency file; CLI 1.0.x commands | v0.7, v0.8 | open | v0.1: plugin requires >= 1.34.0; 1.35.0 resolved | >= 1.34; Container arm64; control-plane API |
| 23 | WCI present in the dev server | v0.7 | open | | skip v0.8 |
| 24 | WCI namespace constraint to `agents` (Phase 3) | v0.9 | open | not expressible on `start-dev` | — |
| 25 | WCI namespace, IDs, activities (Q6); CFN params (6.2); External ID check | v0.8 → v0.12 | open | | — |
| 26 | Native secret injection (AWS Q4) | v0.8 | open | | fetch at startup |
| 27 | AgentCore Terraform resource names (AWS Q6) | v0.8 | open | | AgentCore CLI (ADR-0008) |
| 28 | UI shows the compute config (Q9) | v0.8 / v0.13 | open | | CLI `describe-version` |
| 29 | **Nexus completion wakes the WCI (Q15)** | v0.8 (first), v0.13 (definitive) | open | | nudge Workflow; long debounce; report to Temporal |
| 30 | Chart keys, admintools mounts, frontend labels (2.1, 2.2, 7b.6, Q8) | v0.9 | open | | adjust values; manual PDBs; patch schema job |
| 31 | WCI startup logs; dynamic config reload without restart | v0.9 | open | | rolling restart |
| 32 | arm64 images | v0.9 | open | | m6i.xlarge + amd64 builds |
| 33 | Server cert hot-reload (Q10); SDK file-cert reload (Q18) | v0.9 | open | | Reloader / rolling restart |
| 34 | `TEMPORAL_TLS_*` names; internode EKUs (2.5) | v0.9 | partly resolved | v0.1: env names `TEMPORAL_TLS_CLIENT_CERT_PATH/_DATA`, `TEMPORAL_TLS_CLIENT_KEY_PATH/_DATA`, `TEMPORAL_TLS_SERVER_CA_CERT_PATH/_DATA`, `TEMPORAL_TLS_SERVER_NAME`, `TEMPORAL_TLS_DISABLE_HOST_VERIFICATION` confirmed in the envconfig reference; EKUs at v0.9 | explicit `TLSConfig` |
| 35 | mTLS from Secrets Manager (Q5) | v0.9 (optional), v0.12 | open | | PEM temp files + `_PATH` |
| 36 | kindnet NetworkPolicy | v0.9 | open | | Calico |
| 37 | Module majors; VPC CNI netpol; `force_ssl`; STS endpoint (1.1–1.4) | v0.10 | open | | pin current major; keep endpoint |
| 38 | CDK bootstrap; `CreateServiceLinkedRole` and `PassRole` (3.2); RDS minor | v0.1 doctor + v0.10 | open | v0.1 doctor checks the AWS identity only; CDK/IAM-simulate checks join doctor at v0.10 | bootstrap CDK; grant; record minor |
| 39 | RDS TLS; schema job CA (2.2, 2.3) | v0.11 | open | | `useHelmHooks: false` + manual schema |
| 40 | NLB passthrough (2.4) | v0.11 | open | | — |
| 41 | Global-profile IAM / SCP (3.2) | v0.11, v0.12 | open | v0.1: the SSO admin session can invoke both `global.` and `us.` (not a least-privilege test) | `us.` profile via a variable |
| 42 | CLI VPC runtime + named endpoint (5.4); existing execution role | v0.12 | open | | control-plane API; inline policy |
| 43 | Required endpoints, S3 prefix-list egress (AWS Q2, 1.5) | v0.12 | open | | keep everything; add SG rule |
| 44 | CloudWatch session metrics (AWS Q8) | v0.8, v0.13 | open | | Budgets + `agentcore-sessions.sh` |
| 45 | `agentcore destroy` name (Phase 11) | v0.8 | open | | control-plane deletes |
| 46 | Drainage field, delete-endpoint flags, demotion signal (Phase 10, 2.12) | v1.1 | open | | — |
| 47 | Nexus metric names (Phase 9); External ID rotation (6.3) | v1.2 | open | | documented only |
| 48 | Pod Identity `TagSession` (Q3); hidden dynamic-config keys (Q7); Aurora/OpenSearch (Q11); principal attribution (Q12); XDC (Q14); option (b) ClaimMapper with Nexus (Q17); Code Interpreter IAM (AWS Q5); pricing (AWS Q7) | — | **out of scope** | Recorded as "not exercised" (ADR-0007 for Q17) | IRSA, single cluster, PostgreSQL, option (c) |
