# ADR-0004: Naming, content-hash Build ID, Nexus backing

- Status: Accepted (v0.1). The Nexus backing descope was **confirmed by the user**.
- Runbook: §1.3, §1.4, §3.4, 7b.4. Plan: §3.3.

## Context

Runbook §3.4 left the caller-side and handler-side names, the Build ID scheme and
the Nexus operation backing open. The A2A hop between the two teams must be very
visible in the Web UI and in history, so names carry the story.

## Decision: names

| Item | Value |
|---|---|
| Caller namespace | `agents` (team Support) |
| Caller Worker Deployment | `support-supervisor` |
| Caller task queues | `support-tq` (versioned, serverless-shape Workers only, v0.7+); `support-dev-tq` (unversioned local Supervisor); `nexus-smoke-tq` (`A2ASmokeWorkflow` only) |
| Handler namespace | `agent-services` (team Finance) |
| Handler task queue | `agent-services-tq` |
| Kubernetes namespace (handler Workers) | `temporal-workers` |
| Nexus endpoint | `finance-agent` |
| Nexus service | `FinanceAgentA2A`: `get_agent_card` (sync), `send_message` (workflow-backed) |
| Handler Workflow | `FinanceAgentTaskWorkflow`, ID `a2a-task-<context_id>` |
| Caller tool | `ask_finance_agent` |
| AgentCore runtime / endpoint | `support_supervisor` / `b_<build7>` (matches `[a-zA-Z][a-zA-Z0-9_]{0,47}`) |
| Mock data | order 1042 (headphones, $40, Case A), order 2077 (laptop, $450, Case B); approver `dana.finance` |

**Finance is not a child Workflow** of the Supervisor: child Workflows must live in
the caller's namespace; Nexus is the cross-namespace primitive and the published
contract (`finance_a2a_api`) is the only shared code.

`finance_a2a_api.ENDPOINT` and `.SERVICE` hold the two published names from v0.1.

## Decision: Build ID

`scripts/build-id.sh` (v0.7) computes a **content hash** of
`services/support-agent`, `packages/finance-a2a-api` and the exported Support
dependency set, so docs/infra/Finance-only commits and squash-merges do not change
it. The git SHA travels separately as `SUPPORT_GIT_SHA`. Persistent cloud versions
(v0.12+) are registered only from commits on `main`.

## Decision: Nexus backing

**Workflow-backed only.** The standalone-Activity variant (runbook 3.4, 7b.4:
`TemporalOperationHandler` + `start_activity`, needs `activity.enableCallbacks` and,
per samples-python, a special dev-server build) is **descoped**. The user confirmed
this descope. Q16 is still asked of Temporal (`docs/ask-temporal.md`).

## Consequences

- One handler shape to build, test and operate (samples-python `hello_nexus`
  workflow-run operation pattern).
- Local deviation recorded here: this repo has no GitHub remote yet, so the
  "register from `origin/main`" rule (plan §3.3) is checked against local `main`
  until a remote exists.

## v0.4: the backing as built

- Status line above stands; this section records how the decision was implemented
  and checked at v0.4.
- **Standalone-Activity backing: descoped (user confirmed).** Not built, not tested.
  Q16's Activity half stays a question for Temporal (`docs/ask-temporal.md`).
- **`send_message` is a `@nexus.workflow_run_operation`** that starts
  `FinanceAgentTaskWorkflow` with ID `a2a-task-<context_id>` (samples-python
  `hello_nexus`, `nexus_multiple_args`). `get_agent_card` is a
  `@nexusrpc.handler.sync_operation` with no I/O (well under the 10 s limit).
- **Conflict policy `USE_EXISTING`.** A second `send_message` for the same conversation
  while the task is open joins that Workflow: the SDK sets
  `on_conflict_options.attach_completion_callbacks`, so both callers receive the
  same terminal task. Verified on the dev server: two concurrent operations, one
  Workflow run (`tests/test_a2a_nexus.py`, verify step 11).
- **Reuse policy: the default `ALLOW_DUPLICATE`.** After a task has closed, a
  resend starts a new run of the same Workflow ID. No money moves twice:
  `issue_refund` passes the task ID as the payments idempotency key, and payments
  returns the original refund (`replayed: true`). We rejected
  `REJECT_DUPLICATE` because the caller would get a Nexus error for an honest retry.
- **Validation at the edge.** `send_message` refuses `role != "user"` or a
  `context_id` that is not a short printable token, with `HandlerError(BAD_REQUEST)`
  (non-retryable). Malformed refund data is *not* an edge error: the task ends
  `failed` with a message, as A2A expects.
- **Endpoint.** `scripts/nexus-endpoint.sh` (`make nexus-endpoint`) creates or
  updates `finance-agent` → `agent-services`/`agent-services-tq` with
  `--description-file .generated/finance-agent-card.md`, rendered from
  `FINANCE_AGENT_CARD`. The smoke caller uses its own queue, `nexus-smoke-tq`.
- **Data converter.** Finance's client carries the StrandsPlugin (pydantic
  converter); the no-LLM smoke caller uses the SDK default. The contract round-trips
  through all four pairings (`packages/finance-a2a-api/tests/test_roundtrip.py`).
