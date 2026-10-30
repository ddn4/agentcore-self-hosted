# Changelog

One section per tag. Tags are never moved; fixes go forward as `v0.N.1-…`.

## v0.7-serverless-shape

The Support Worker in AgentCore Runtime shape, on the laptop: built to die, pinned to its build.

- `support_agent.agentcore_app`: `BedrockAgentCoreApp` entrypoint (single-start guard,
  `add_async_task` -> `create_task` -> immediate ack, WCI payload checked against this
  version, `complete_async_task` in `finally`); one Worker lifetime = mTLS (no-op locally) ->
  connect with `TEMPORAL_CONNECT_TIMEOUT_SECONDS` -> versioned Worker on `support-tq`
  (`support-supervisor:<BUILD_ID>`, PINNED, 120 s drain) -> `ActivityTracker` debounce
  (`AGENTCORE_DEBOUNCE_SECONDS`, 20 s locally). `PingWorkflow`, `worker_versioned`, `mtls`.
  Shape of samples-python `bedrock_agentcore/strands_agent`.
- Content-hash Build ID (`scripts/build-id.sh`; refuses dirty inputs). CodeZip bundle
  (`make agentcore-bundle`: flat, vendored contract, `requirements.txt` + generated
  `pyproject.toml`, `BUILD_ID`, rendered `agentcore.json` with endpoint `b_<build7>`);
  `make agentcore-bundle-test` (fresh 3.12 venv, serve, connect timeout, 43 MB arm64 zip).
- **The dev server contains the WCI** (ledger 23). `make dev-server WCI=1` +
  `scripts/agentcore_emulator.py` let the real WCI invoke the local Worker with no AWS
  contact; `register-version.sh`, `wci-history.sh`, `serverless_report.py`.
  `make invoke-local` is the plan's "I am the WCI" path.
- Answers: 7.1 `create` is required; `create-version` invokes the runtime (ledger 20); Q13
  informational: no `--allow-no-pollers` needed for a version with a compute config (21);
  Q4/CodeZip local half (22); Q6 WCI IDs/namespace/activities locally (25); Q15 local first
  look: a Nexus completion wakes the WCI (29).
- Tests: 319 in `make check` (283 at v0.6): entrypoint contract with a fake app, debounce
  with a fake clock, mTLS, PingWorkflow on a versioned Worker and the full entrypoint
  lifecycle on `start_local`, Build ID stability in a throwaway clone, `agentcore.json`
  rendering, emulator routes. CI builds and tests the bundle.
- `scripts/verify/v0.7-serverless-shape.sh`: Case B across two Worker lifetimes, pinned to
  one Build ID, invoked by hand and by the WCI.
- Live Bedrock still deferred (AWS SSO session expired; ledger 49 carried to v0.8).

## v0.6-human-in-the-loop

Finance's human approver, and a caller that can wait for it at no cost.

- Finance (`FinanceAgentTaskWorkflow`): over $100 or at HIGH risk the A2A task goes
  `input-required` and waits, durably, for `approve`/`reject` signals
  (`ApproverDecision(approver, comment)`; sync handlers; decisions outside `input-required`
  ignored) for at most `FINANCE_APPROVAL_TIMEOUT` (default 24h, labelled timer); no decision
  → `failed`; approve → refund with `approved_by=<approver>`. "Current details" in the Web UI
  show the task state. The timeout travels in the task's memo (ledger 58).
- Contract: `APPROVAL_WAIT_LIMIT` (24 h) published in `finance_a2a_api` and in the Agent
  Card (v1.1.0); Finance refuses a larger `FINANCE_APPROVAL_TIMEOUT`.
- Support: `SUPPORT_NEXUS_SEND_TIMEOUT` (default 25h) is the third Workflow input and the
  `send_message` budget (ADR-0009). `rejected` → apology, `failed` → apology + escalation,
  `NexusOperationError` → apology (explicit "Next step" in the tool result; prompts updated).
- ADR-0002 (HITL in code, not Strands `interrupt()`) and ADR-0009 (timeout budget) accepted.
- Tests: 283 in `make check` (208 at v0.5). Finance time-skipping: approve, reject, 24 h
  timeout, RISK=HIGH on $40, idempotency key, out-of-place signals. Support (stub Finance with
  the approval rule): approve/reject in both modes, `failed` → escalation, caller budget too
  short → Nexus timeout with the Finance task orphaned. Two namespaces: approve, reject,
  Support Worker stopped mid-wait, end-to-end timeout (5s/60s). `tests/test_timeout_budget.py`.
- Demo: `make approve`, `make reject`, `make pending`, `make result`, `make approval-report`;
  `make case-b` starts and returns; `scripts/verify/v0.6-human-in-the-loop.sh`
  (`VERIFY_REAL_WAIT_MINUTES=31`: approved after 31 real minutes with Support stopped).
- Found: no time skipping through an open Nexus operation on the v1 test server (ledger 56,
  `make spike-nexus-wait`); the server caps a Nexus budget at the caller's execution timeout
  (57); an optional trailing `run()` parameter untypes a dataclass input (58). Reported in
  `ask-temporal.md`.
- Live Bedrock still deferred (AWS SSO session expired; ledger 49 carried to v0.7).

## v0.5-a2a-over-nexus

The Supervisor asks Finance itself: A2A over Nexus from inside the agent loop.

- `support_agent/finance_peer.py`: Agent Card discovery (`get_agent_card`) before the first
  Supervisor turn; `send_message` with a 25 h budget and operation summaries naming peer,
  order, amount and mode; `NexusOperationError` reported to the Supervisor, not raised.
- `ask_finance_agent`: async in-workflow `@tool` awaiting the Nexus operation (**Q19a: works**;
  G1 not triggered). `SUPPORT_A2A_MODE=handoff`: unforced capture tool `submit_refund_request`,
  Nexus call in `run()`, second Supervisor turn. The mode is a Workflow input.
- Prompt-drift guard: one re-prompt if eligible and Finance not contacted. New `a2a` query.
- Tests: 199 in `make check` (172 at v0.4). Time-skipping with a stub Finance behind the real
  contract (`demo_testing.finance_stub`); two-namespace Supervisor → real Finance; crash after
  Finance answered (no repeat); sync-tool probe; v0.5 replay fixtures; v0.3 fixtures kept as
  negatives. `test_live_a2a.py` (`-m bedrock`, Case A ×5) and `tests/e2e/test_refund.py`
  (`make e2e`, profile-driven).
- Demo: `make case-a|case-b` honour `SUPPORT_A2A_MODE`; `make timeline`, `make a2a`,
  `make task-state WF=support-…`, `make e2e`; `make dev` runs the Support Worker and applies
  the endpoint. `scripts/verify/v0.5-a2a-over-nexus.sh`, `scripts/check_a2a_run.py`.
- Found: a sync Strands `@tool` fails quietly inside a Workflow (`asyncio.to_thread`);
  reported to Temporal. ADR-0003 part 2 accepted; ledger 15–17 resolved (17 live half pending).
- Verifier fixes: the drift guard no longer reads "NOT ELIGIBLE"/"NOT-ELIGIBLE" as eligible
  (`is_eligible()`, unit-tested; 208 tests); `make task-state` works on a rejected task
  (CLI prints empty artifacts as `null`).
- Live Bedrock still deferred (AWS SSO session expired; ledger 49 carried to v0.6).

## v0.4-finance-agent

Another team's agent behind a published A2A contract, reached over Nexus (no LLM on the caller).

- `finance_a2a_api`: A2A 0.3.0-shaped dataclasses (snake_case; A2A 1.0.0 deltas in its
  README), `@nexusrpc.service FinanceAgentA2A` (`get_agent_card` sync, `send_message`
  workflow-backed), `FINANCE_AGENT_CARD` + `render_markdown()`. Round-trips through the default
  and pydantic converters, 7b.9 CLI JSON, schema-snapshot gate, Agent Card golden.
- Finance (`agent-services`): `FinanceAgentA2AHandler` (`a2a-task-<context_id>`, `USE_EXISTING`),
  `FinanceAgentTaskWorkflow` (A2A task states, `get_task` query, auto-approve only: over $100 or
  HIGH risk -> `rejected` until v0.6), Finance Refund agent (`fraud_score`, `refund_history`),
  approval rule in code, idempotent `issue_refund`; `make worker-finance[-scripted]`.
- `payments-mock`: idempotent refund API (stdlib), `make payments-mock` on :8081.
- Support: `A2ASmokeWorkflow` + `make worker-smoke` (`nexus-smoke-tq`); Nexus operation summaries.
- `make nexus-endpoint` (endpoint description = Agent Card), `make smoke`, `make card`
  (standalone Nexus Operation from the CLI), `make task-state`, `scripts/a2a_timeline.py`,
  `make dev` runs payments-mock, Finance and smoke Workers.
- Dev server and test fixture enable standalone Nexus operations
  (`nexusoperation.enableStandalone=true`; off by default in CLI 1.9.1, contrary to the docs).
- Contract module uses real annotations (string annotations broke decoding in the caller's sandbox).
- Answered: A2A field names (pinned 0.3.0), Q16 CLI on 1.32.0, Q21 (yes), Nexus on time-skipping
  (v1 yes, v2 no), 10 s sync limit, idempotency. ADR-0004 implementation section.
- Tests: 172 in `make check` (62 at v0.3), incl. cross-namespace Nexus with the real endpoint
  name; replay fixtures `smoke-1042`, `a2a-task-smoke-1042`. `scripts/verify/v0.4-finance-agent.sh`.
- Live Bedrock still deferred (AWS SSO session expired; ledger 49 carried to v0.5).

## v0.3-sub-agents

Support's own team of agents, used as tools, in-process.

- `support_agent/agents.py`: Order Lookup agent (`lookup_order`) and Return Policy agent
  (`get_return_policy`) as `TemporalAgent`s, each built fresh inside a hand-written
  in-workflow `@tool`; Supervisor (runbook 5.6 prompt minus Finance) uses only those two
  tools. `summary` per agent attributes every `invoke_model` in the UI.
- Workflow query `sub_agent_runs`; return policy rule 5 (over $100: Finance manager
  approval), so Case B (2077) is escalated.
- Q20 resolved (ADR-0003 part 1): nesting works; Strands `Agent.as_tool()` writes durable
  timers in a Workflow (cancel-signal poll), one shared sub-agent refuses parallel calls.
  Reported in `docs/ask-temporal.md`; runbook annotated.
- `demo_testing`: per-system-prompt `support_responder`, Activity summaries in history
  helpers, probe Workflows for `as_tool()` and a shared instance.
- Tests: nested sequence/attribution, per-agent prompts and tools, Case B, parallel
  sub-agent calls, failing sub-agent, crash inside a sub-agent turn, re-recorded
  dev-server fixtures, v0.2 fixture asserted non-replayable, live `bedrock` tests.
- `spikes/nested_agents.py` (`make spike-nested`), `make case-b`, `make sub-agents`,
  `scripts/verify/v0.3-sub-agents.sh` (includes v0.2's carried live steps).
- Live Bedrock still deferred (AWS SSO session expired; ledger 49 carried to v0.4).

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
- Offline wire test (`test_wire_offline.py`): the signed ConverseStream request from the
  pinned model carries the Opus 5.5 ID, no sampling params, `toolChoice` auto, and effort
  only when not `medium`.
- Live Bedrock checks deferred to v0.3 (ledger 49; AWS SSO session expired at this tag).

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
