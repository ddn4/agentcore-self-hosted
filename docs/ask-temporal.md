# Questions and reports for Temporal

Collected as tags answer (or fail to answer) them. Sent together with the Q15
result (v0.8/v0.13). Numbers are runbook §7 question numbers.

| Q | Question | Status | Notes |
|---|---|---|---|
| Q15 | Does a Workflow Task produced by a Nexus completion callback reliably trigger the WCI when nothing is polling the caller's versioned task queue? | open: first answer v0.8, definitive v0.13 | report with WCI history and timeline |
| Q16 | Does stock server 1.32.1 support Nexus operations backed by a standalone Activity (`activity.enableCallbacks`, `TemporalOperationHandler.start_activity`)? Standalone Nexus Operations from the CLI? | ask | the Activity-backed variant is **descoped** for this demo (ADR-0004); CLI part checked at v0.4 (1.32.0) and v0.11 (1.32.1) |
| Q19 (2nd half) | Is a `nexus_operation_as_tool` planned for the Strands plugin, like the OpenAI Agents integration's? | ask | the demo uses a hand-written async `@tool` (v0.5) |
| Q4 | Minimum `temporalio` for a plain AgentCore Worker? | open | v0.1: `temporalio-strands-agents` 0.1.0 requires >= 1.34.0 |
| Q5 | Recommended pattern for self-hosted mTLS from AgentCore (envconfig + Secrets Manager)? | open | v0.9 / v0.12 |
| Q13 | Does `set-current-version` for AgentCore versions need `--allow-no-pollers`? | open | v0.7 informational, v0.12 definitive |
| Q21 | Can a Python caller attach a summary to a Nexus operation for the Web UI? | open | v0.4 |

## Reports (non-questions)

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
