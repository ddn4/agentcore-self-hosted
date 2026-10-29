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

- v0.1: the skill/docs examples use `ClientConfig.load_client_connect_config()` with
  `setdefault("target_host", ...)`; this repo does the same and adds a per-service
  namespace default. No issue to report.
