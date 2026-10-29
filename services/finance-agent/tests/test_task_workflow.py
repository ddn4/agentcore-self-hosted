"""FinanceAgentTaskWorkflow (the A2A task) with a scripted Finance Refund agent.

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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from temporalio.client import Client, WorkflowHistory
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from demo_testing.finance_script import finance_responder
from demo_testing.history import activity_types, attributed_activities
from demo_testing.scripted_model import ScriptedModel, client_with_plugin, scripted_plugin
from finance_a2a_api import A2AMessage, A2ATask, first_data, refund_request, status_text, text_part
from finance_agent import agents
from finance_agent.worker import WORKFLOWS, build_worker
from finance_agent.workflows import FinanceAgentTaskWorkflow
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


async def run_task(
    env: WorkflowEnvironment,
    executor: ThreadPoolExecutor,
    msg: A2AMessage,
    model: ScriptedModel | None = None,
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
            id=f"a2a-task-{msg.context_id}",
            task_queue=task_queue,
        )
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


async def test_over_limit_is_rejected_without_moving_money(
    time_skipping_env: WorkflowEnvironment,
    activity_executor: ThreadPoolExecutor,
    payments: PaymentsServer,
) -> None:
    task, history, _ = await run_task(time_skipping_env, activity_executor, message("2077", 450.0))
    assert task.status.state == "rejected"
    assert "needs a Finance approver" in status_text(task) and "$450.00" in status_text(task)
    assert task.artifacts == []
    assert "issue_refund" not in activity_types(history)
    assert payments.ledger.all() == []


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
    model = ScriptedModel(finance_responder(agents.FINANCE_PROMPT, risk_override=override))
    task, _, _ = await run_task(time_skipping_env, activity_executor, message("1042", 40.0), model)
    assert task.status.state == "rejected", reason
    assert "risk HIGH" in status_text(task)
    assert payments.ledger.all() == []


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
