# ADR-0009: Nexus timeout budget (caller 25 h > Finance's 24 h approval wait)

- Status: **Accepted (v0.6).**
- Runbook: 5.6 (`schedule_to_close_timeout=timedelta(hours=24)` on the caller), 7b.4
  (`wait_condition(..., timeout=timedelta(hours=24))` on the handler).
- Plan: §3.3 "Timeouts (ADR-0009)", v0.6; verify ledger item 19.

## Context

The runbook gives both sides the same 24 h. Whichever expires first decides what Support
sees:

| Expires first | Support sees | Finance's task |
|---|---|---|
| Finance's approval wait | `send_message` **completes** with an A2A task in state `failed` ("No Finance approver decided within 24h") | ended, `failed`, no refund |
| Support's `schedule_to_close_timeout` | `NexusOperationError` (cause `TimeoutError: operation timed out`, event `NexusOperationTimedOut`) | **still running**, waiting for an approver; nobody on Support's side will hear about the decision |

Equal values race. The Nexus docs confirm that a schedule-to-close timeout ends the
operation for the caller only; it is cancellation (not a timeout) that propagates to the
handler Workflow ("Nexus Operations › Timeouts", "› Cancelation"). v0.6 observed the
orphan directly (`test_caller_budget_shorter_than_finance_wait_is_a_nexus_timeout`: the
stub task is still `RUNNING` after the caller timed out).

## Decision

1. **Finance publishes its limit.** `finance_a2a_api.APPROVAL_WAIT_LIMIT = 24 h` is part of
   the contract, and the Agent Card (v1.1.0) says it in words ("waits in state
   input-required for up to 24 h, then ends failed"). `FINANCE_APPROVAL_TIMEOUT` (default
   24h) may be shorter, never longer: `finance_agent.config.approval_timeout()` refuses a
   larger value at Worker start.
2. **Support budgets above it.** `SUPPORT_NEXUS_SEND_TIMEOUT` defaults to the published
   limit + 1 h = **25h**, and is the `send_message` `schedule_to_close_timeout`. A human who
   never answers therefore comes back as Finance's own `failed` answer, which the
   Supervisor turns into an apology and an escalation.
3. **Support still handles `NexusOperationError` explicitly** (v0.5, `FinancePeer.ask`):
   endpoint misconfigured, Finance refusing the request, or a budget set too short. The
   exchange is recorded as `unreachable`, and the Supervisor apologises and promises no
   refund ("a member of the Finance team will follow up"). Every terminal state reaches the
   Supervisor with an explicit "Next step" (`finance_peer.NEXT_STEP`).
4. **How the values reach Workflow code** (Workflow code can't read the environment):
   - Support: the starter (`scripts/demo/case-a.sh`/`case-b.sh`, `tests/e2e`) passes
     `SUPPORT_NEXUS_SEND_TIMEOUT` as the **third Workflow input** (`"25h"`), so the budget
     is in history and visible in the UI. All three inputs are strings, so starts with two
     inputs (v0.5 histories, older scripts) still decode.
   - Finance: the Nexus handler passes `FINANCE_APPROVAL_TIMEOUT` in the task Workflow's
     **memo** (`approval_timeout`), read with `workflow.memo_value(...)`. Not a second
     `run()` input: the Python SDK uses type hints only when the number of payloads equals
     the number of parameters (`_workflow_instance.py::_convert_payloads`), so with an
     optional second parameter every older task (one payload) would decode its
     `A2AMessage` as a `dict` and fail on replay. The v0.4 replay fixture caught this; the
     Temporal docs and community answers describe the same fallback and recommend a single
     dataclass input. The A2A message *is* the single input here, and it belongs to the
     contract, so the memo carries Finance-private configuration instead.
5. **Invariant test.** `tests/test_timeout_budget.py` (unit) asserts
   `SUPPORT_NEXUS_SEND_TIMEOUT ≥ FINANCE_APPROVAL_TIMEOUT + 1h` for the code defaults and
   `.env.example`, and that Finance refuses anything above the published limit. At run time
   the Supervisor logs a warning when its budget is not above the published limit
   (allowed: tests and demos shorten it).

## Consequences

- **The server caps an operation's budget at the caller's remaining run time.** With a
  120 s `execution_timeout` on the Support Workflow, `describe` shows the `send_message`
  `scheduleToCloseTimeout` as `120s`, not 25 h (`test_support_worker_stopped_while_finance_waits`).
  The demo scripts start Support with no execution timeout, so the full 25 h applies
  (`make pending` shows `90000s`). Any future execution or run timeout on the Supervisor
  must stay above 25 h, or it silently becomes the Nexus budget.
- Q19b (no Strands tool timeout) holds, so the only bound on `ask_finance_agent` is this
  budget. A 31-minute real wait with the Support Worker stopped is recorded in the v0.6
  story doc.
- Cancellation is not used: if Support is cancelled, the Nexus machinery cancels Finance's
  task; a timeout would not. That is another reason to never let Support's budget be the
  first to expire.
- In Temporal Cloud the maximum schedule-to-close is 60 days; on the self-hosted server it is
  `component.nexusoperations.limit.scheduleToCloseTimeout` (dynamic config). 25 h is far
  below both; check the self-hosted value at v0.9/v0.11 if it is ever lowered.
