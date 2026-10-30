"""Workflows for team Finance (handler), namespace ``agent-services``.

``FinanceAgentTaskWorkflow`` **is** the A2A task (runbook §1.4, 7b.4): one
Workflow per Support conversation, ID ``a2a-task-<context_id>``, started by the
``FinanceAgentA2A.send_message`` Nexus operation. Its state is the A2A
``Task`` (``submitted -> working -> [input-required ->] completed | rejected |
failed``), readable at any time with the ``get_task`` query; its result, the
terminal ``A2ATask``, is the Nexus operation's result that the server delivers
back to the caller's namespace.

Steps:

1. Validate the request (a data part ``{order_id, amount_usd, reason}``); an
   invalid request ends the task ``failed``.
2. The Finance Refund agent (Strands ``TemporalAgent``) calls ``fraud_score``
   and ``refund_history`` and recommends ``RISK=LOW|HIGH``.
3. ``policy.decide`` (code, not the model) allows an automatic refund only up
   to $100 at LOW risk. **v0.6: anything else is a human decision.** The task
   goes to ``input-required`` and the Workflow waits, durably, for an
   ``approve`` or ``reject`` signal (``ApproverDecision``) for at most the
   approval timeout (``FINANCE_APPROVAL_TIMEOUT``, default 24 h, set by the Nexus
   handler in the memo ``approval_timeout``). Reject ends the task ``rejected``;
   no decision in time ends it
   ``failed``; approve carries on (ADR-0002: HITL enforced in code, not with
   Strands ``interrupt()``).
4. ``issue_refund`` calls payments with the task ID as idempotency key and the
   task completes with a ``refund_confirmation`` artifact naming the approver.

Shape follows samples-python ``hello_nexus/handler/workflows.py`` (a plain
Workflow started by a workflow-run operation; contract types imported through
the sandbox), ``strands_plugin/tools/workflow.py`` (agent built in
``__init__``, ``invoke_async`` in ``run``), ``message_passing/introduction``
(sync signal handlers that only set state, a dataclass signal input, a query
that never mutates) and ``strands_plugin/human_in_the_loop`` (the Workflow,
not the model, waits for the human with ``workflow.wait_condition``). The
timeout follows the temporal-developer skill's "Wait Condition with Timeout"
pattern (``asyncio.TimeoutError``, which is the builtin ``TimeoutError`` on 3.11+).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Literal

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

from finance_agent.agents import FINANCE_AGENT_NAME, new_finance_agent
from finance_agent.durations import format_duration, parse_duration
from finance_agent.policy import (
    ApproverDecision,
    RefundRequest,
    decide,
    parse_refund_request,
    parse_risk,
)

with workflow.unsafe.imports_passed_through():
    from finance_a2a_api import (
        APPROVAL_WAIT_LIMIT,
        AUTO_APPROVE_LIMIT_USD,
        A2AMessage,
        A2ATask,
        Artifact,
        TaskState,
        TaskStatus,
        data_part,
        first_data,
        first_text,
        text_part,
    )
    from finance_agent.activities import IssueRefundInput, issue_refund, say_hello

ISSUE_REFUND_START_TO_CLOSE = timedelta(seconds=30)
#: Payments may be briefly down: retry with backoff for ~5 minutes, then fail the task.
ISSUE_REFUND_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=12,
)
#: ``approved_by`` when no human was needed.
AUTO_APPROVER = "auto"
#: Default approval wait when the handler passes none: the published limit (24h).
DEFAULT_APPROVAL_TIMEOUT = format_duration(APPROVAL_WAIT_LIMIT)
#: Memo key the Nexus handler sets to the Worker's ``FINANCE_APPROVAL_TIMEOUT`` ("24h").
#:
#: A memo, not a second ``run()`` parameter: the SDK decodes Workflow inputs with
#: type hints only when the number of payloads equals the number of parameters,
#: so with an optional second parameter every task started before v0.6 (one
#: payload) would decode its ``A2AMessage`` as a plain ``dict`` and fail on replay
#: (``temporalio/worker/_workflow_instance.py`` ``_convert_payloads``; found by the
#: v0.4 replay fixture). The memo is in the start event, so it replays too.
APPROVAL_TIMEOUT_MEMO = "approval_timeout"
#: Timer label in the Web UI / CLI for the approval wait.
APPROVAL_TIMER = "Finance approval wait"

Verdict = Literal["approve", "reject"]


def _activity_failure(err: BaseException) -> ActivityError | None:
    """The ActivityError behind ``err`` (Strands may wrap the model Activity's error)."""
    seen: BaseException | None = err
    while seen is not None:
        if isinstance(seen, ActivityError):
            return seen
        seen = seen.__cause__ or seen.__context__
    return None


@workflow.defn
class FinanceAgentTaskWorkflow:
    """The Finance Refund agent handling one A2A task."""

    @workflow.init
    def __init__(self, msg: A2AMessage) -> None:
        # The task exists before run() starts, so queries and the approve/reject
        # signals always see it (temporal-developer python.md, @workflow.init).
        self._task = A2ATask(
            id=workflow.info().workflow_id,
            context_id=msg.context_id,
            status=TaskStatus(state="submitted", timestamp=workflow.now().isoformat()),
        )
        self._decision: tuple[Verdict, ApproverDecision] | None = None
        self.agent = new_finance_agent()

    # --- A2A task state ------------------------------------------------------

    def _set(self, state: TaskState, text: str | None = None) -> None:
        message = None
        if text:
            message = A2AMessage(
                role="agent",
                parts=[text_part(text)],
                message_id=str(workflow.uuid4()),
                context_id=self._task.context_id,
                task_id=self._task.id,
            )
        self._task.status = TaskStatus(
            state=state, message=message, timestamp=workflow.now().isoformat()
        )
        # Shown as "Current details" on the Workflow page of the Web UI.
        workflow.set_current_details(
            f"A2A task state: **{state}**" + (f"\n\n{text}" if text else "")
        )
        workflow.logger.info(
            "A2A task %s -> %s%s", self._task.id, state, f": {text}" if text else ""
        )

    def _finish(self, state: TaskState, text: str) -> A2ATask:
        self._set(state, text)
        return self._task

    # --- the task --------------------------------------------------------------

    @workflow.run
    async def run(self, msg: A2AMessage) -> A2ATask:
        approval_timeout = workflow.memo_value(
            APPROVAL_TIMEOUT_MEMO, DEFAULT_APPROVAL_TIMEOUT, type_hint=str
        )
        try:
            wait = parse_duration(approval_timeout)
        except ValueError as err:
            return self._finish("failed", f"Finance is misconfigured: approval timeout {err}.")
        req = parse_refund_request(first_data(msg.parts))
        if isinstance(req, str):
            return self._finish("failed", f"Invalid refund request: {req}.")

        self._set(
            "working", f"{FINANCE_AGENT_NAME} is assessing the refund for order {req.order_id}."
        )
        try:
            result = await self.agent.invoke_async(self._prompt(req, first_text(msg.parts)))
        except Exception as err:
            failure = _activity_failure(err)
            if failure is None:
                raise
            return self._finish("failed", f"Risk assessment failed: {failure.cause or failure}.")
        assessment = str(result).strip()
        risk = parse_risk(assessment)

        approved_by = AUTO_APPROVER
        if decide(req.amount_usd, risk) == "needs_approver":
            why = (
                f"over the ${AUTO_APPROVE_LIMIT_USD:.0f} auto-approval limit"
                if req.amount_usd > AUTO_APPROVE_LIMIT_USD
                else "risk HIGH"
            )
            # A2A input-required: the agent needs input from a human before it can go on.
            self._set(
                "input-required",
                f"Refund of ${req.amount_usd:.2f} for order {req.order_id} needs a Finance "
                f"approver ({why}; risk {risk}). Waiting up to {format_duration(wait)} for "
                f"an approve or reject signal on {self._task.id}. Assessment: {assessment}",
            )
            try:
                await workflow.wait_condition(
                    lambda: self._decision is not None,
                    timeout=wait,
                    timeout_summary=APPROVAL_TIMER,
                )
            except TimeoutError:
                return self._finish(
                    "failed",
                    f"No Finance approver decided within {format_duration(wait)}: no refund "
                    f"was issued for order {req.order_id} (${req.amount_usd:.2f}).",
                )
            assert self._decision is not None
            verdict, decision = self._decision
            note = f": {decision.comment}" if decision.comment else ""
            if verdict == "reject":
                return self._finish(
                    "rejected",
                    f"Refund of ${req.amount_usd:.2f} for order {req.order_id} was rejected "
                    f"by Finance approver {decision.approver}{note}. No refund was issued.",
                )
            approved_by = decision.approver
            self._set("working", f"Approved by {decision.approver}{note}; issuing the refund.")
        else:
            self._set("working", f"Approved automatically (risk {risk}); issuing the refund.")

        try:
            receipt = await workflow.execute_activity(
                issue_refund,
                IssueRefundInput(
                    order_id=req.order_id,
                    amount_usd=req.amount_usd,
                    # The task ID: the same on every retry and replay, so one refund at most.
                    idempotency_key=self._task.id,
                ),
                start_to_close_timeout=ISSUE_REFUND_START_TO_CLOSE,
                retry_policy=ISSUE_REFUND_RETRY,
                summary=f"Payments: refund order {req.order_id} ${req.amount_usd:.2f}",
            )
        except ActivityError as err:
            return self._finish(
                "failed", f"The payments system did not issue the refund: {err.cause or err}."
            )

        self._task.artifacts.append(
            Artifact(
                artifact_id=f"{self._task.id}-refund_confirmation",
                name="refund_confirmation",
                parts=[
                    data_part(
                        {
                            "refund_id": receipt.refund_id,
                            "order_id": receipt.order_id,
                            "amount_usd": receipt.amount_usd,
                            "approved_by": approved_by,
                            "risk": risk,
                            "handled_by": receipt.handled_by,
                        }
                    )
                ],
            )
        )
        how = (
            f"auto-approved: within the ${AUTO_APPROVE_LIMIT_USD:.0f} limit at {risk} risk"
            if approved_by == AUTO_APPROVER
            else f"approved by Finance approver {approved_by}"
        )
        return self._finish(
            "completed",
            f"Refund {receipt.refund_id} issued for ${receipt.amount_usd:.2f} on order "
            f"{receipt.order_id} ({how}).",
        )

    @staticmethod
    def _prompt(req: RefundRequest, support_text: str) -> str:
        return (
            f"Refund request from the Customer Support agent: order {req.order_id}, "
            f"amount ${req.amount_usd:.2f}, reason: {req.reason or 'not given'}.\n"
            f"Support says: {support_text or '(no message)'}"
        )

    # --- the human approver (signals) and the task (query) ---------------------

    @workflow.signal
    def approve(self, decision: ApproverDecision) -> None:
        """A Finance approver approves the refund (only while the task is input-required)."""
        self._decide("approve", decision)

    @workflow.signal
    def reject(self, decision: ApproverDecision) -> None:
        """A Finance approver rejects the refund (only while the task is input-required)."""
        self._decide("reject", decision)

    def _decide(self, verdict: Verdict, decision: ApproverDecision) -> None:
        # Signals can't answer the sender, so anything out of place is logged and
        # ignored: the first decision while input-required wins (scripts/demo/
        # approve.sh checks the state with get_task before signalling).
        state = self._task.status.state
        approver = decision.approver.strip()
        if state != "input-required":
            workflow.logger.warning("Ignoring %s from %r: task is %s", verdict, approver, state)
        elif self._decision is not None:
            workflow.logger.warning(
                "Ignoring %s from %r: %s already decided", verdict, approver, self._decision[1]
            )
        elif not approver:
            workflow.logger.warning("Ignoring %s without an approver name", verdict)
        else:
            self._decision = (verdict, ApproverDecision(approver, decision.comment.strip()))

    @workflow.query
    def get_task(self) -> A2ATask:
        """The A2A Task as it stands now (``temporal workflow query --type get_task``)."""
        return self._task


@workflow.defn
class HelloWorkflow:
    """Trivial Workflow that proves the Worker, namespace and test harness work (v0.1)."""

    @workflow.run
    async def run(self, name: str) -> str:
        return await workflow.execute_activity(
            say_hello, name, start_to_close_timeout=timedelta(seconds=10)
        )
