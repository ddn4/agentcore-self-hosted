"""Workflows for team Finance (handler), namespace ``agent-services``.

``FinanceAgentTaskWorkflow`` **is** the A2A task (runbook §1.4, 7b.4): one
Workflow per Support conversation, ID ``a2a-task-<context_id>``, started by the
``FinanceAgentA2A.send_message`` Nexus operation. Its state is the A2A
``Task`` (``submitted -> working -> completed | rejected | failed``), readable
at any time with the ``get_task`` query; its result, the terminal ``A2ATask``,
is the Nexus operation's result that the server delivers back to the caller's
namespace.

Steps:

1. Validate the request (a data part ``{order_id, amount_usd, reason}``); an
   invalid request ends the task ``failed``.
2. The Finance Refund agent (Strands ``TemporalAgent``) calls ``fraud_score``
   and ``refund_history`` and recommends ``RISK=LOW|HIGH``.
3. ``policy.decide`` (code, not the model) allows an automatic refund only up
   to $100 at LOW risk. **v0.4 is auto-approve only**: anything else ends the
   task ``rejected`` ("needs a Finance approver"); v0.6 adds ``input-required``
   with ``approve``/``reject`` signals and an approval timeout.
4. ``issue_refund`` calls payments with the task ID as idempotency key and the
   task completes with a ``refund_confirmation`` artifact.

Shape follows samples-python ``hello_nexus/handler/workflows.py`` (a plain
Workflow started by a workflow-run operation; contract types imported through
the sandbox) and ``strands_plugin/tools/workflow.py`` (agent built in
``__init__``, ``invoke_async`` in ``run``); the ``get_task`` query follows
``message_passing`` / ``strands_plugin/human_in_the_loop``.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

from finance_agent.agents import FINANCE_AGENT_NAME, new_finance_agent
from finance_agent.policy import RefundRequest, decide, parse_refund_request, parse_risk

with workflow.unsafe.imports_passed_through():
    from finance_a2a_api import (
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
#: ``approved_by`` when no human was needed (v0.6 adds the approver's name).
AUTO_APPROVER = "auto"


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
        # The task exists before run() starts, so queries (and, from v0.6, the
        # approve/reject signals) always see it (temporal-developer python.md).
        self._task = A2ATask(
            id=workflow.info().workflow_id,
            context_id=msg.context_id,
            status=TaskStatus(state="submitted", timestamp=workflow.now().isoformat()),
        )
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
        workflow.logger.info(
            "A2A task %s -> %s%s", self._task.id, state, f": {text}" if text else ""
        )

    def _finish(self, state: TaskState, text: str) -> A2ATask:
        self._set(state, text)
        return self._task

    # --- the task --------------------------------------------------------------

    @workflow.run
    async def run(self, msg: A2AMessage) -> A2ATask:
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

        if decide(req.amount_usd, risk) == "needs_approver":
            return self._finish(
                "rejected",
                f"Refund of ${req.amount_usd:.2f} for order {req.order_id} needs a Finance "
                f"approver (auto-approval limit ${AUTO_APPROVE_LIMIT_USD:.0f}, risk {risk}); "
                f"approvals are not enabled on this agent yet. Assessment: {assessment}",
            )

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
                            "approved_by": AUTO_APPROVER,
                            "risk": risk,
                            "handled_by": receipt.handled_by,
                        }
                    )
                ],
            )
        )
        return self._finish(
            "completed",
            f"Refund {receipt.refund_id} issued for ${receipt.amount_usd:.2f} on order "
            f"{receipt.order_id} (auto-approved: within the ${AUTO_APPROVE_LIMIT_USD:.0f} "
            f"limit at {risk} risk).",
        )

    @staticmethod
    def _prompt(req: RefundRequest, support_text: str) -> str:
        return (
            f"Refund request from the Customer Support agent: order {req.order_id}, "
            f"amount ${req.amount_usd:.2f}, reason: {req.reason or 'not given'}.\n"
            f"Support says: {support_text or '(no message)'}"
        )

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
