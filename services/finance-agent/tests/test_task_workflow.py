"""FinanceAgentTaskWorkflow (the A2A task) with a scripted Finance Refund agent.

v0.6 adds the human approval: ``input-required``, the ``approve``/``reject``
signals and the approval timeout. The time-skipping server makes the 24 h wait
take milliseconds (temporal-developer testing.md: time-skipping for durable
timers); signals and queries follow testing.md "Testing Signals and Queries".

The ``workflow`` layer of plan §5: time-skipping test server, no AWS, no Nexus
(the Nexus hop is covered by ``tests/test_a2a_nexus.py`` on the dev server).
The ScriptedModel sits under the same ``"bedrock"`` key Opus 5.5 is pinned to.
``issue_refund`` is the real Activity against a real payments-mock on an
ephemeral port, so idempotency is exercised end to end. Shape follows
samples-python ``tests/strands_plugin/tools_test.py``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowHandle, WorkflowHistory
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from demo_testing.a2a_tasks import wait_for_task_state
from demo_testing.finance_script import finance_responder
from demo_testing.history import activity_types, attributed_activities
from demo_testing.scripted_model import ScriptedModel, client_with_plugin, scripted_plugin
from finance_a2a_api import A2AMessage, A2ATask, first_data, refund_request, status_text, text_part
from finance_agent import agents
from finance_agent.policy import ApproverDecision
from finance_agent.worker import WORKFLOWS, build_worker
from finance_agent.workflows import (
    APPROVAL_TIMEOUT_MEMO,
    APPROVAL_TIMER,
    FinanceAgentTaskWorkflow,
)
from payments_mock.server import PaymentsServer

pytestmark = pytest.mark.workflow

AGENT = f"invoke_model [{agents.FINANCE_AGENT_NAME}]"
HISTORIES = Path(__file__).parent / "histories"
# Loaded at collection time (sync): no blocking file I/O inside the async tests.
RECORDED = {p.stem: json.loads(p.read_text()) for p in sorted(HISTORIES.glob("*.json"))}


def message(order_id: str, amount: float, context_id: str | None = None) -> A2AMessage:
    return refund_request(
        message_id="m-1",
        context_id=context_id or f"support-{order_id}-{uuid.uuid4().hex[:8]}",
        order_id=order_id,
        amount_usd=amount,
        reason="damaged",
        summary=f"Customer reports order {order_id} arrived damaged.",
    )


Approver = Callable[[WorkflowHandle[Any, A2ATask]], Awaitable[None]]


def approve_as(name: str, comment: str = "") -> Approver:
    """Wait for input-required, then signal ``approve`` (what scripts/demo/approve.sh does)."""

    async def act(handle: WorkflowHandle[Any, A2ATask]) -> None:
        await wait_for_task_state(handle, "input-required")
        await handle.signal(FinanceAgentTaskWorkflow.approve, ApproverDecision(name, comment))

    return act


def reject_as(name: str, comment: str = "") -> Approver:
    async def act(handle: WorkflowHandle[Any, A2ATask]) -> None:
        await wait_for_task_state(handle, "input-required")
        await handle.signal(FinanceAgentTaskWorkflow.reject, ApproverDecision(name, comment))

    return act


async def run_task(
    env: WorkflowEnvironment,
    executor: ThreadPoolExecutor,
    msg: A2AMessage,
    model: ScriptedModel | None = None,
    *,
    approver: Approver | None = None,
    approval_timeout: str = "24h",
) -> tuple[A2ATask, WorkflowHistory, ScriptedModel]:
    model = model or ScriptedModel(finance_responder(agents.FINANCE_PROMPT))
    client: Client = client_with_plugin(env.client, scripted_plugin(model))
    task_queue = f"finance-test-{uuid.uuid4()}"
    async with build_worker(
        client, task_queue=task_queue, activity_executor=executor, max_cached_workflows=0
    ):
        handle = await client.start_workflow(
            FinanceAgentTaskWorkflow.run,
            msg,
            memo={APPROVAL_TIMEOUT_MEMO: approval_timeout},
            id=f"a2a-task-{msg.context_id}",
            task_queue=task_queue,
        )
        if approver is not None:
            await approver(handle)
        task = await handle.result()
        # The query reads the same task object the Workflow returned.
        assert await handle.query(FinanceAgentTaskWorkflow.get_task) == task
        history = await handle.fetch_history()
    return task, history, model


async def test_small_low_risk_refund_completes(
    time_skipping_env: WorkflowEnvironment,
    activity_executor: ThreadPoolExecutor,
    payments: PaymentsServer,
) -> None:
    msg = message("1042", 40.0)
    task, history, model = await run_task(time_skipping_env, activity_executor, msg)

    assert task.id == f"a2a-task-{msg.context_id}" and task.context_id == msg.context_id
    assert task.status.state == "completed"
    assert task.status.message is not None and task.status.message.role == "agent"
    assert task.status.message.task_id == task.id
    (artifact,) = task.artifacts
    assert artifact.name == "refund_confirmation"
    confirmation = first_data(artifact.parts)
    assert confirmation is not None
    (refund,) = payments.ledger.all()
    assert confirmation["refund_id"] == refund.refund_id
    assert refund.idempotency_key == task.id  # the A2A task ID is the payments key
    assert confirmation["amount_usd"] == 40.0 and confirmation["risk"] == "LOW"
    assert confirmation["approved_by"].startswith("auto")
    assert refund.refund_id in status_text(task)

    # The agent's model turns and tools, then the refund (issued by code).
    assert attributed_activities(history) == [
        AGENT,  # -> fraud_score + refund_history (parallel)
        "fraud_score",
        "refund_history",
        AGENT,  # RISK=LOW ...
        "issue_refund [Payments: refund order 1042 $40.00]",
    ]
    assert [c.system_prompt for c in model.calls] == [agents.FINANCE_PROMPT] * 2
    assert all(c.tool_choice is None for c in model.calls)  # never forced (Opus 5.5)


def _approval_timers(history: WorkflowHistory) -> list[float]:
    return [
        e.timer_started_event_attributes.start_to_fire_timeout.ToTimedelta().total_seconds()
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_TIMER_STARTED
        and APPROVAL_TIMER in str(e.user_metadata.summary.data)
    ]


async def test_over_limit_waits_for_approval_then_completes(
    time_skipping_env: WorkflowEnvironment,
    activity_executor: ThreadPoolExecutor,
    payments: PaymentsServer,
) -> None:
    """$450 -> input-required -> approve(dana.finance) -> completed, one refund."""
    msg = message("2077", 450.0)
    seen: list[A2ATask] = []

    async def approve(handle: WorkflowHandle[Any, A2ATask]) -> None:
        seen.append(await wait_for_task_state(handle, "input-required"))
        assert payments.ledger.all() == [], "no money moves before the approver decides"
        await handle.signal(
            FinanceAgentTaskWorkflow.approve, ApproverDecision("dana.finance", "photos checked")
        )

    task, history, _ = await run_task(time_skipping_env, activity_executor, msg, approver=approve)

    (waiting,) = seen
    assert "needs a Finance approver" in status_text(waiting)
    assert "over the $100 auto-approval limit" in status_text(waiting)
    assert f"approve or reject signal on {waiting.id}" in status_text(waiting)
    assert task.status.state == "completed"
    (refund,) = payments.ledger.all()
    assert refund.idempotency_key == task.id  # the A2A task ID is the payments key
    confirmation = first_data(task.artifacts[0].parts)
    assert confirmation is not None
    assert confirmation["approved_by"] == "dana.finance"
    assert confirmation["refund_id"] == refund.refund_id and confirmation["amount_usd"] == 450.0
    assert "approved by Finance approver dana.finance" in status_text(task)
    # One durable timer: the approval wait, labelled, 24 h.
    assert _approval_timers(history) == [24 * 3600]
    assert attributed_activities(history)[-1] == (
        "issue_refund [Payments: refund order 2077 $450.00]"
    )


async def test_reject_ends_the_task_rejected_without_moving_money(
    time_skipping_env: WorkflowEnvironment,
    activity_executor: ThreadPoolExecutor,
    payments: PaymentsServer,
) -> None:
    task, history, _ = await run_task(
        time_skipping_env,
        activity_executor,
        message("2077", 450.0),
        approver=reject_as("dana.finance", "duplicate claim"),
    )
    assert task.status.state == "rejected"
    assert "rejected by Finance approver dana.finance: duplicate claim" in status_text(task)
    assert task.artifacts == []
    assert "issue_refund" not in activity_types(history)
    assert payments.ledger.all() == []


async def test_no_decision_within_the_timeout_fails_the_task(
    time_skipping_env: WorkflowEnvironment,
    activity_executor: ThreadPoolExecutor,
    payments: PaymentsServer,
) -> None:
    """Nobody answers: after 24 h (skipped) the task ends failed, no refund."""
    task, history, _ = await run_task(time_skipping_env, activity_executor, message("2077", 450.0))
    assert task.status.state == "failed"
    assert "No Finance approver decided within 24h" in status_text(task)
    assert payments.ledger.all() == []
    assert _approval_timers(history) == [24 * 3600]
    fired = [e for e in history.events if e.event_type == EventType.EVENT_TYPE_TIMER_FIRED]
    assert len(fired) == 1
    started = history.events[0].event_time.ToDatetime()
    assert history.events[-1].event_time.ToDatetime() - started >= timedelta(hours=24)


async def test_approval_timeout_comes_from_the_memo(
    time_skipping_env: WorkflowEnvironment,
    activity_executor: ThreadPoolExecutor,
    payments: PaymentsServer,
) -> None:
    task, history, _ = await run_task(
        time_skipping_env, activity_executor, message("2077", 450.0), approval_timeout="5s"
    )
    assert task.status.state == "failed" and "within 5s" in status_text(task)
    assert _approval_timers(history) == [5]


@pytest.mark.parametrize(
    ("override", "reason"),
    [("HIGH", "the model says HIGH"), ("", "the model gives no RISK verdict (fail safe)")],
)
async def test_risk_gate_is_enforced_in_code(
    time_skipping_env: WorkflowEnvironment,
    activity_executor: ThreadPoolExecutor,
    payments: PaymentsServer,
    override: str,
    reason: str,
) -> None:
    """RISK=HIGH on $40 (under the limit) still needs a human: input-required."""
    model = ScriptedModel(finance_responder(agents.FINANCE_PROMPT, risk_override=override))
    seen: list[A2ATask] = []

    async def look_then_reject(handle: WorkflowHandle[Any, A2ATask]) -> None:
        seen.append(await wait_for_task_state(handle, "input-required"))
        await handle.signal(FinanceAgentTaskWorkflow.reject, ApproverDecision("dana.finance"))

    task, _, _ = await run_task(
        time_skipping_env,
        activity_executor,
        message("1042", 40.0),
        model,
        approver=look_then_reject,
    )
    assert "risk HIGH" in status_text(seen[0]), reason
    assert task.status.state == "rejected"
    assert payments.ledger.all() == []


async def test_out_of_place_signals_are_ignored(
    time_skipping_env: WorkflowEnvironment,
    activity_executor: ThreadPoolExecutor,
    payments: PaymentsServer,
) -> None:
    """A blank approver is ignored; the first real decision wins; later ones are ignored."""

    async def noisy(handle: WorkflowHandle[Any, A2ATask]) -> None:
        await wait_for_task_state(handle, "input-required")
        await handle.signal(FinanceAgentTaskWorkflow.approve, ApproverDecision("  "))
        await handle.signal(FinanceAgentTaskWorkflow.reject, ApproverDecision("dana.finance"))
        await handle.signal(FinanceAgentTaskWorkflow.approve, ApproverDecision("eve"))

    task, _, _ = await run_task(
        time_skipping_env, activity_executor, message("2077", 450.0), approver=noisy
    )
    assert task.status.state == "rejected" and "dana.finance" in status_text(task)
    assert payments.ledger.all() == []


async def test_approve_before_input_required_does_not_preapprove(
    time_skipping_env: WorkflowEnvironment,
    activity_executor: ThreadPoolExecutor,
    payments: PaymentsServer,
) -> None:
    """A signal that arrives while the agent is still assessing is ignored."""

    async def early(handle: WorkflowHandle[Any, A2ATask]) -> None:
        await handle.signal(FinanceAgentTaskWorkflow.approve, ApproverDecision("too.early"))

    task, _, _ = await run_task(
        time_skipping_env,
        activity_executor,
        message("2077", 450.0),
        approver=early,
        approval_timeout="1h",
    )
    assert task.status.state == "failed", "the early approval must not count"
    assert payments.ledger.all() == []


async def test_bad_approval_timeout_fails_the_task(
    time_skipping_env: WorkflowEnvironment,
    activity_executor: ThreadPoolExecutor,
) -> None:
    task, history, model = await run_task(
        time_skipping_env, activity_executor, message("2077", 450.0), approval_timeout="soon"
    )
    assert task.status.state == "failed" and "misconfigured" in status_text(task)
    assert model.calls == [] and activity_types(history) == []


async def test_invalid_request_fails_before_any_model_call(
    time_skipping_env: WorkflowEnvironment,
    activity_executor: ThreadPoolExecutor,
) -> None:
    msg = A2AMessage(
        role="user", parts=[text_part("refund please")], message_id="m", context_id="c-1"
    )
    task, history, model = await run_task(time_skipping_env, activity_executor, msg)
    assert task.status.state == "failed"
    assert "no data part" in status_text(task)
    assert model.calls == [] and activity_types(history) == []


async def test_payments_refusal_fails_the_task(
    time_skipping_env: WorkflowEnvironment,
    activity_executor: ThreadPoolExecutor,
    payments: PaymentsServer,
) -> None:
    msg = message("1042", 40.0)
    # Someone already used this task's key for a different amount: payments says 409.
    payments.ledger.issue(
        idempotency_key=f"a2a-task-{msg.context_id}", order_id="1042", amount_usd=1.0
    )
    task, history, _ = await run_task(time_skipping_env, activity_executor, msg)
    assert task.status.state == "failed"
    assert "did not issue the refund" in status_text(task)
    assert activity_types(history)[-1] == "issue_refund"


async def test_model_failure_fails_the_task(
    time_skipping_env: WorkflowEnvironment,
    activity_executor: ThreadPoolExecutor,
) -> None:
    """An invoke_model that keeps failing (e.g. an HTTP 400) ends the A2A task as failed."""

    def broken(_call: object) -> str:
        raise RuntimeError("simulated Bedrock 400")

    task, history, _ = await run_task(
        time_skipping_env, activity_executor, message("1042", 40.0), ScriptedModel(broken)
    )
    assert task.status.state == "failed"
    assert "Risk assessment failed" in status_text(task)
    assert activity_types(history) == ["invoke_model"]


async def test_replays_from_history(
    time_skipping_env: WorkflowEnvironment,
    activity_executor: ThreadPoolExecutor,
    payments: PaymentsServer,
) -> None:
    _, history, _ = await run_task(time_skipping_env, activity_executor, message("1042", 40.0))
    replayer = Replayer(workflows=WORKFLOWS, plugins=[scripted_plugin(ScriptedModel([]))])
    await replayer.replay_workflow(history)


def test_recorded_fixture_exists() -> None:
    assert RECORDED, "record with: RECORD_HISTORY=1 scripts/verify/v0.4-finance-agent.sh"


@pytest.mark.parametrize("workflow_id", sorted(RECORDED))
async def test_recorded_dev_server_histories_replay(workflow_id: str) -> None:
    """Histories recorded from the dev server by ``scripts/verify/v0.4-finance-agent.sh``."""
    history = WorkflowHistory.from_json(workflow_id, RECORDED[workflow_id])
    replayer = Replayer(workflows=WORKFLOWS, plugins=[scripted_plugin(ScriptedModel([]))])
    await replayer.replay_workflow(history)
    assert attributed_activities(history)[-1].startswith("issue_refund")
