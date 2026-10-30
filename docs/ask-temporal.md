# Questions and reports for Temporal

Collected as tags answer (or fail to answer) them. Sent together with the Q15
result (v0.8/v0.13). Numbers are runbook §7 question numbers.

| Q | Question | Status | Notes |
|---|---|---|---|
| Q15 | Does a Workflow Task produced by a Nexus completion callback reliably trigger the WCI when nothing is polling the caller's versioned task queue? | open: first AWS answer v0.8, definitive v0.13; **local first look (v0.7): yes** | v0.7 on the dev server with a local AgentCore API emulator: `task-add-signal` → `InvokeWorker` 0.22 s after the approval that completed Finance's task (story v0.7 step 10). Report with WCI history and timeline |
| Q16 | Does stock server 1.32.1 support Nexus operations backed by a standalone Activity (`activity.enableCallbacks`, `TemporalOperationHandler.start_activity`)? Standalone Nexus Operations from the CLI? | ask (Activity half); CLI half answered for 1.32.0 at v0.4 | Activity-backed variant **descoped** (ADR-0004). CLI on 1.32.0: works once `nexusoperation.enableStandalone=true` is set (see the v0.4 report below); re-check 1.32.1 at v0.11 |
| Q19 (1st half) | Does `temporalio-strands-agents` run an `async` plain `@tool` on the Workflow event loop, so it can `await` a Nexus operation? | **resolved v0.5: yes** (0.1.0 / strands-agents 1.62.0) | Strands awaits a coroutine tool directly (`strands/tools/decorator.py`); the Nexus operation is scheduled from inside the Supervisor's turn and the history replays (`test_workflow.py`, `tests/test_supervisor_a2a.py`, verify v0.5). Strands' tool executor has no timeout of its own (source read; the multi-hour wait is v0.6, Q19b). No question to send, but see the sync-tool report below |
| Q19 (2nd half) | Is a `nexus_operation_as_tool` planned for the Strands plugin, like the OpenAI Agents integration's (`temporalio.openai_agents.workflow.nexus_operation_as_tool`)? | ask | Not in `temporalio-strands-agents` 0.1.0 (no Nexus code in `temporalio/strands_agents/`); the docs list it only for OpenAI Agents. The demo uses a hand-written async `@tool` (`support_agent/agents.py` + `finance_peer.py`, ~40 lines incl. the A2A message) |
| Q4 | Minimum `temporalio` for a plain AgentCore Worker? | ask | v0.1: `temporalio-strands-agents` 0.1.0 requires >= 1.34.0. v0.7: the APIs used (`WorkerDeploymentConfig` with PINNED default) exist since Python SDK 1.11 per the docs; nothing in the SDK is AgentCore-specific. Is anything newer needed for WCI-invoked Workers? |
| Q5 | Recommended pattern for self-hosted mTLS from AgentCore (envconfig + Secrets Manager)? | open | v0.9 / v0.12 |
| Q13 | Does `set-current-version` for AgentCore versions need `--allow-no-pollers`? | v0.7 informational answer; v0.12 definitive | Dev server 1.32.0: **no**, for a version with a compute config (never polled, or after the Worker drained). Ask whether that is by design (a compute config counts as "can get pollers"), since the Lambda samples always pass the flag |
| Q21 | Can a Python caller attach a summary to a Nexus operation for the Web UI? | **resolved v0.4: yes** | `execute_operation(..., summary=...)` (temporalio 1.35.0) sets `user_metadata.summary` on `NexusOperationScheduled`; no question to send |

## Reports (non-questions)

- v0.4 (CLI 1.9.1, server 1.32.0): **Standalone Nexus Operations are disabled on the
  dev server by default.** The docs ("Standalone Nexus Operation › Temporal CLI support"
  and the per-SDK "Standalone Nexus Operations" pages) say `temporal server start-dev`
  "enables Standalone Nexus Operations by default". With CLI 1.9.1,
  `temporal nexus operation start|execute` fails with `Standalone Nexus operation is
  disabled` until the dev server is started with `--dynamic-config-value
  nexusoperation.enableStandalone=true` (the key from the 1.32.0 release notes).
  The Python SDK's `client.create_nexus_client(...).execute_operation` fails the same way.
  Repro: `temporal server start-dev --namespace a --namespace b`, create an endpoint, run
  `temporal nexus operation start -n a ...`. Suggest fixing the docs or the dev-server
  default.
- v0.4 (temporalio 1.35.0): **a dataclass module with `from __future__ import
  annotations`, used as a Nexus operation's output type, broke the caller's Workflow
  Task.** `_apply_resolve_nexus_operation` → default converter → `value_to_type` →
  `typing.get_type_hints(AgentCard)` raised `NameError: name 'AgentSkill' is not
  defined` inside the sandbox ("Failed decoding arguments"). The same decode works
  outside the sandbox, and the module was imported with
  `workflow.unsafe.imports_passed_through()`. The Workflow Task retried until the
  run timed out. Workaround: real (non-string) annotations in the contract module.
  Worth a docs note or a sandbox fix, because `from __future__ import annotations` is
  common in shared "contract" packages.
- v0.4: **Nexus works on the v1 time-skipping test server** in our test (temporalio
  1.35.0's test server, single namespace), though samples-python still skips its
  Nexus tests there ("Nexus tests don't work under the Java test server").
  `start_time_skipping_v2()` against CLI 1.9.1 fails with "The Time-Skipping feature is
  not enabled for namespace". Which dynamic-config key enables it on the dev server?
  (`spikes/nexus_time_skipping.py`)

- v0.3 (temporalio-strands-agents 0.1.0, strands-agents 1.62.0): **Strands'
  `Agent.as_tool()` writes durable timers when used inside a Workflow.** The adapter
  forwards the parent's `cancel_signal` (a `threading.Event`). `Agent.stream_async`
  mirrors it with `_link_cancel_signal`, which polls `asyncio.sleep(0.05)`, and in a
  Workflow that is a timer. On the dev server we saw about one `TimerStarted`/`TimerFired`
  pair (plus a Workflow Task) per second of sub-agent model time. On the time-skipping
  test server the timers advance virtual time while the sub-agent's model Activity
  runs. One run wrote 18 timers for a 1 s turn, and two runs hung. Nesting itself
  works well with a hand-written `@tool` that awaits `TemporalAgent.invoke_async` (Q20,
  ADR-0003). Suggest that the plugin neutralise the cancel watcher for `TemporalAgent`
  (or provide a `TemporalAgent.as_tool`), or that the docs show the hand-written
  pattern. Also worth documenting: one sub-agent instance cannot serve two tool calls
  in the same turn (`ConcurrencyException`). Offline repro:
  `services/support-agent/tests/test_agents_as_tools.py`, `make spike-nested`.

- v0.2 (temporalio-strands-agents 0.1.0, strands-agents 1.62.0): **structured output
  loses its provider fallback across the Activity boundary.** `TemporalAgent(
  structured_output_model=X)` makes Strands retry with a forced `toolChoice` when the
  model answers in text. Plain Strands then lets `BedrockModel` degrade forced → native
  `outputConfig` → auto for models that refuse forced tool use (Opus 5.5, Sonnet 5.5,
  Fable 5.1). The plugin's `_InvokeModelInput` does not carry `structured_output_model`
  and `TemporalModel` has no `_supports_structured_output`, so the forced request is
  sent as is and fails with a 400 on those models. Suggest forwarding
  `structured_output_model` (by import path) to `invoke_model`, or documenting the
  limitation. Workaround here: an unforced capture tool (ADR-0001 §6). Offline repro:
  `services/support-agent/tests/test_structured_output_path.py`.

- v0.1: the skill/docs examples use `ClientConfig.load_client_connect_config()` with
  `setdefault("target_host", ...)`; this repo does the same and adds a per-service
  namespace default. No issue to report.
- v0.5 (temporalio-strands-agents 0.1.0, strands-agents 1.62.0): **a sync Strands
  `@tool` fails quietly inside a Workflow.** Strands runs a non-coroutine tool with
  `asyncio.to_thread`; Temporal's Workflow event loop refuses `run_in_executor`, so the
  call raises `NotImplementedError`, which Strands turns into an error tool result
  (`"Error: NotImplementedError - "`). The Workflow does not fail; the model just never
  gets the tool's answer. samples-python `strands_plugin/tools` uses exactly such a
  tool (`letter_counter`) and the docs present it as the in-workflow pattern. Repro:
  `services/support-agent/tests/test_tool_kinds.py` (sandboxed Workflow, scripted
  model). Suggest: run sync tools inline in the plugin (they are meant to be
  deterministic), or document "in-workflow tools must be `async def`" and fix the sample.
- v0.6 (temporalio 1.35.0, CLI 1.9.1 test server via `start_time_skipping()`): **the
  time-skipping test server does not skip time while a caller has an async Nexus
  operation open.** A caller Workflow awaits a workflow-backed operation whose handler
  Workflow waits 24 h (`wait_condition(timeout=24h)`). Waiting on the caller's result
  does not advance time, and `env.sleep(timedelta(hours=24))` fails with
  `RPCError: Timeout expired`; the handler's timer fires in real time. Started directly
  (no Nexus), the same handler Workflow skips 24 h in ~10 ms. So "Nexus under
  time-skipping" (v0.4, ledger 13) works for quick operations only; long human waits
  behind Nexus can't be unit-tested in virtual time. Is that expected, and is
  `start_time_skipping_v2()` meant to cover it (it is not enabled on CLI 1.9.1's dev
  server)? Repro: `make spike-nexus-wait` (`spikes/nexus_long_wait_time_skipping.py`).
- v0.6 (temporalio 1.35.0): **an optional trailing `run()` parameter silently untypes the
  first one.** `_convert_payloads` drops all type hints when the payload count differs
  from the parameter count, so adding `approval_timeout: str = "24h"` after a dataclass
  input made older histories decode the dataclass as a `dict` (replay failure). This
  matches the "single dataclass input" advice, but a warning (or per-position hints when
  fewer payloads than parameters arrive) would make the failure obvious. We moved the
  value to the memo (ADR-0009).
- v0.6 (server 1.32.0): **a Nexus operation's schedule-to-close is capped at the caller's
  remaining run time** (`describe` shows `120s` for a 25 h budget on a Workflow started with
  `execution_timeout=120s`). Sensible, but not in the Nexus timeout docs; worth a line
  there, since a caller-side execution timeout then becomes the A2A budget.
- v0.7 (CLI 1.9.1 / server 1.32.0): **the dev server contains the WCI, and its AWS SDK honours
  `AWS_ENDPOINT_URL`**, so AgentCore Serverless Workers can be exercised end to end on a
  laptop with a small stand-in for `GetAgentRuntimeEndpoint` and `InvokeAgentRuntime`
  (`scripts/agentcore_emulator.py`), as the LocalStack guide does for Lambda. Worth a docs
  page for AgentCore ("Test a Serverless Worker locally"). Two details we had to discover on
  the wire: the Go SDK rejects numeric `createdAt`/`lastUpdatedAt` in the endpoint response
  (must be ISO strings), and the invoke path is `/runtimes/<runtime id>/invocations?accountId=…&qualifier=…`.
- v0.7: **`create-version` invokes the runtime** (WCI `InvokeWorkersToRegisterTaskQueues`) and
  fails with only "something went wrong, please retry (<code>)" when the runtime cannot be
  reached. A clearer error ("could not invoke the AgentCore endpoint: …") would help, and the
  docs could say that `create-version` and `update-version-compute-config` start one session.
  `create-version` on an uncreated deployment says "does your Worker Deployment have pollers?",
  which reads oddly for a serverless version; `create` is required (runbook 7.1).
- v0.7: **the WCI Workflow is not visible in visibility listings** (`temporal workflow list`
  finds nothing) but `temporal workflow show -w temporal-sys-worker-controller-instance:<deployment>:<build>`
  works in the deployment's namespace. Documenting the ID format and the Activity names
  (`ValidateSpec`, `InvokeWorkersToRegisterTaskQueues`, `InvokeWorker`, signal
  `task-add-signal`) would answer Q6 for self-hosters (our "Validate Connection").
- v0.7: the WCI sometimes invokes again while a Worker of the version is still polling
  (1.7 s after a conversation ended); the sample's single-start guard absorbs it. Is a
  redundant invoke expected with `no-sync`?
- v0.9 (chart temporal-1.7.0): **admintools, the namespace Job and the helm test present the
  frontend server certificate as their client certificate** (`temporal.adminClient.tls.*`).
  With `requireClientAuth` this forces `client auth` onto the frontend cert and puts the
  frontend private key into the admintools pod. An `admintools.tls.secretName` (a dedicated
  client cert) would avoid both. Also: `values.tls.yaml` says nothing about this, while
  `ci/postgres-tls-values.yaml` silently issues the frontend cert with both EKUs.
- v0.9: **`nexusoperation.enableStandalone` is still off by default on server 1.32.1**
  (Helm), like on the dev server 1.32.0 (ledger 12). Same docs question as at v0.4.
- v0.9: the chart's pods carry `app.kubernetes.io/version=1.32.0` (appVersion) when
  `server.image.tag` is overridden to 1.32.1; deriving the label from the image tag would
  make dashboards less confusing.
- v0.9 (Python SDK): rotating a file-based client cert needs a new `Client` assigned to
  `worker.client` (docs). An envconfig option to re-read `_PATH` files on reconnect would
  fit cert-manager setups (we restart the Finance Deployment instead).
