# ADR-0002: Human approval is enforced in Workflow code, not with Strands `interrupt()`

- Status: **Accepted (v0.6).**
- Runbook: §1.4 (A2A task states, "Response to `input-required`"), 7b.4 ("Design choice:
  the LLM recommends, code enforces"; "Alternative for the approval step").
- Plan: v0.6; verify ledger items 18 and 19.

## Context

Refunds above $100, or at HIGH fraud risk, need a human Finance approver. The A2A task
must show `input-required` while it waits, and Support must be able to wait for hours
(the serverless point of the demo). Two ways exist to pause a Strands agent on Temporal:

| Option | How it pauses | Who decides that a human is needed |
|---|---|---|
| **A. Workflow code** (runbook 7b.4 sketch) | The agent finishes its turn with `RISK=LOW\|HIGH`; `policy.decide()` (a plain `if`) picks auto-approve or `input-required`; `run()` waits with `workflow.wait_condition(..., timeout=)` for an `approve`/`reject` signal | Code: `amount > 100 or risk == "HIGH"` |
| B. Strands `interrupt()` (samples-python `strands_plugin/human_in_the_loop`) | A `BeforeToolCallEvent` hook on an `issue_refund` tool calls `event.interrupt(...)`; the agent stops with `stop_reason == "interrupt"`; the Workflow waits for a signal and resumes the agent with an interrupt response | The hook, but only when the **model** chooses to call `issue_refund` |

## Decision

**Option A.** The Finance Refund agent only recommends (`RISK=` plus a justification for
the approver). Whether money moves without a human is a deterministic `if` in Workflow
code (`finance_agent/policy.py::decide`), and the approval itself is a durable wait in
`FinanceAgentTaskWorkflow.run()`:

- The task goes to `input-required` with a message naming the amount, why a human is
  needed, the wait limit and the signal to send.
- Signals `approve` / `reject` take one dataclass, `ApproverDecision(approver, comment="")`
  (samples-python `message_passing/introduction` `ApproveInput`; the Python docs recommend
  a single dataclass so fields can be added). From the CLI:
  `--input '{"approver": "dana.finance"}'` (`make approve` / `make reject`).
- Handlers are **sync** and only set state (temporal-developer `patterns.md`: signal
  handlers should generally be non-async). A decision outside `input-required`, a second
  decision, or a blank approver is logged and ignored. Signals cannot answer the sender, so
  `scripts/demo/approve.sh` reads `get_task` first and refuses unless the task is
  `input-required`.
- The wait is `workflow.wait_condition(lambda: decision is not None, timeout=..., timeout_summary="Finance approval wait")`.
  `TimeoutError` (the skill's "Wait Condition with Timeout" pattern) ends the task `failed`.
  The timeout is `FINANCE_APPROVAL_TIMEOUT` (default 24 h), passed by the Nexus handler in
  the task's memo (ADR-0009 explains why not as an input).
- On approve, `issue_refund` runs with the task ID as the payments idempotency key, and the
  `refund_confirmation` artifact names the approver (`approved_by`).

## Why not B

- **The rule would depend on the model.** With B the hook fires only if the model calls
  `issue_refund`. A model that "decides" a $450 refund is fine and answers in text never
  reaches the gate. With A the gate is in code, whatever the model writes; a missing or
  unclear `RISK=` counts as HIGH (`test_risk_gate_is_enforced_in_code`).
- **The A2A state is explicit.** A sets `input-required` itself, so `get_task`, the Web UI
  "current details" and the Nexus result all show the same state. B would need extra code
  to map `stop_reason == "interrupt"` onto the A2A state anyway.
- **The talk track.** "The LLM recommends, code enforces" is the point to make in a refund
  demo (runbook 7b.4).
- **Fewer moving parts across the Activity boundary.** B resumes the agent with an
  interrupt response, which is one more Strands feature whose state has to survive replay
  inside the plugin. A uses only Temporal primitives (signal, timer, query).

B stays a good fit when a tool call itself is the risky act and the model should keep its
context across the pause; the sample shows it.

## Consequences

- The agent's conversation ends before the wait. The approver reads the assessment in the
  task message; the agent is not asked again after the decision.
- An Update with a validator would let the approver get "rejected: not waiting" back
  synchronously. The plan specifies signals and the CLI path is simpler; `approve.sh`'s
  state check covers the demo. Revisit if an approver UI is built.
- Evidence: `services/finance-agent/tests/test_task_workflow.py` (approve, reject, 24 h
  timeout skipped, RISK=HIGH on $40, out-of-place signals, early approval ignored),
  `tests/test_supervisor_a2a.py`, `scripts/verify/v0.6-human-in-the-loop.sh` steps 5–9.
