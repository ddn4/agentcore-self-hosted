# Changelog

One section per tag. Tags are never moved; fixes go forward as `v0.N.1-…`.

## v0.2-durable-agent

An LLM loop as a Workflow that survives a crash.

- `SupportSupervisorWorkflow`: `TemporalAgent(model="bedrock")` with `lookup_order` and
  `get_return_policy` as `activity_as_tool`; model Activity heartbeat 10 s, bounded retry,
  UI summary; `transcript` query. Sync data Activities on a thread pool; mock orders
  1042/2077/3301 and the return policy.
- `worker_factory.build_worker()` + `make worker-support` (`support-dev-tq`).
- `BEDROCK_EFFORT` → `output_config.effort` (not sent at the default `medium`), both services.
- `demo_testing`: `ScriptedModel` under the `"bedrock"` key (text, tool calls, `Hang`,
  responder functions, delay), history helpers, structured-output probe Workflow.
- Tests: activities, scripted agent loop, pinned key, unknown model key, tool errors,
  replay (committed history from a real `kill -9` run), crash safety across two Workers,
  structured output's forced retry.
- Spikes: `opus_params.py` (wire, 400s, effort field and latency) and
  `opus_structured_output.py` (forced vs capture tool) through the plugin's own
  `invoke_model` Activity. **Live run pending** (AWS SSO session expired).
- Demo: `make case-a`, `make worker-support-scripted`, `make record-histories`, `make spikes`;
  `scripts/verify/v0.2-durable-agent.sh` (`kill -9` a real Worker mid-`invoke_model`).
- ADR-0001 amended: effort field; no `structured_output_model` on Opus 5.5 (capture tool).

## v0.1-scaffold

A green, trustworthy repo before the first line of agent code.

- uv workspace (Python 3.12): `packages/finance-a2a-api`, `packages/demo-testing`,
  `services/support-agent`, `services/finance-agent`, `services/payments-mock`.
- Per-service `config.py`: envconfig connection (`TEMPORAL_CONFIG_FILE` /
  `TEMPORAL_PROFILE`), and `StrandsPlugin(models={"bedrock": ...})` pinning
  Opus 5.5 (`global.anthropic.claude-opus-5-5`, `us-east-1`) with no inference
  params (ADR-0001).
- Trivial `HelloWorkflow` + `say_hello` Activity per service, tested on a
  two-namespace dev server (`start_local`, CLI 1.9.1) and on the time-skipping server.
- `demo_testing.fixtures`: session-wide two-namespace dev server, per-namespace clients.
- Makefile: `setup doctor fmt lint types test test-unit test-workflow test-integration
  check scrub-check scrub-history test-live bedrock-ping dev-server dev-server-up
  dev-server-down dev-server-reset dev hello-smoke verify`.
- Scripts: `doctor.sh`, `dev-server.sh`, `bedrock_ping.py`, `hello_smoke.py`,
  `scrub-check.sh` (gitleaks if installed, grep scrub always), `redact.sh`,
  `verify/v0.1-scaffold.sh`.
- CI workflow file `.github/workflows/ci.yml` (`make check` + gitleaks; not run yet: no remote).
- Docs: `versions.md`, `verify-ledger.md` (seeded from plan §6; 6 items resolved),
  `ask-temporal.md`, ADR-0001, ADR-0004 (Nexus Activity backing descoped, user-confirmed),
  ADR-0007, story `v0.1-scaffold.md`.
- Verification pass: `test_plugin_worker.py` per service (HelloWorkflow on the Worker with the
  pinned StrandsPlugin and pydantic converter); `hello_smoke.py` uses the plugin; gitleaks tree
  scan limited to committable files.
