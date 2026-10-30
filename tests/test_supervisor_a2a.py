"""v0.5: the Support Supervisor asks the real Finance agent, across namespaces.

Plan §5 "Nexus (2 namespaces)" layer for v0.5. On the shared two-namespace dev
server (``start_local``):

- caller: ``SupportSupervisorWorkflow`` on the Support Worker exactly as
  ``support_agent.worker`` builds it, with the scripted Support team under
  ``"bedrock"`` (namespace ``agents``);
- handler: the Finance Worker exactly as ``finance_agent.worker`` builds it, with
  the scripted Finance agent under ``"bedrock"`` (namespace ``agent-services``);
- the real endpoint name ``finance-agent``; a real payments-mock.

Both A2A modes, Case A ($40 -> refund issued) and, from v0.6, Case B ($450 ->
Finance ``input-required`` -> a human approves or rejects with a signal), including
the plan's "Support Worker stopped while Finance waits" path. Shape follows
``tests/test_a2a_nexus.py`` (v0.4) and samples-python
``tests/hello_nexus/hello_nexus_test.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any

import pytest
import pytest_asyncio
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowHistory

from demo_testing import AGENT_SERVICES_NAMESPACE
from demo_testing.a2a_tasks import wait_for_a2a_task
from demo_testing.finance_script import finance_responder
from demo_testing.history import nexus_operations, timeline
from demo_testing.nexus import create_nexus_endpoint, delete_nexus_endpoint
from demo_testing.scripted_model import ScriptedModel, client_with_plugin, scripted_plugin
from demo_testing.support_script import SupportPrompts, support_responder
from finance_a2a_api import ENDPOINT, A2ATask, render_markdown
from finance_agent import agents as finance_agents
from finance_agent.policy import ApproverDecision
from finance_agent.worker import build_worker as build_finance_worker
from finance_agent.workflows import FinanceAgentTaskWorkflow
from payments_mock.server import PaymentsServer, serve_in_thread
from support_agent import agents
from support_agent.worker_factory import build_worker as build_support_worker
from support_agent.workflows import SupportSupervisorWorkflow

pytestmark = [
    pytest.mark.integration,
    # Strands async generators of evicted Workflows are collected outside their loop.
    pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning"),
]

FINANCE_TQ = f"agent-services-tq-test-{uuid.uuid4().hex[:8]}"
SUPPORT_TQ = f"support-tq-test-{uuid.uuid4().hex[:8]}"
PROMPTS = SupportPrompts(
    agents.SUPERVISOR_PROMPT,
    agents.ORDER_LOOKUP_PROMPT,
    agents.RETURN_POLICY_PROMPT,
    agents.SUPERVISOR_HANDOFF_PROMPT,
)
CASE_A = "Order #1042, my headphones arrived broken. I want my money back."
CASE_B = "Order #2077: my laptop arrived with a cracked screen. I want a refund."


@pytest.fixture(scope="module")
def payments() -> Iterator[tuple[PaymentsServer, str]]:
    server, url = serve_in_thread()
    try:
        yield server, url
    finally:
        server.shutdown()
        server.server_close()


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def teams(
    agents_client: Client,
    agent_services_client: Client,
    payments: tuple[PaymentsServer, str],
) -> AsyncIterator[tuple[Client, Client]]:
    """Endpoint + Finance Worker + Support Worker for this module; yields (support, finance)."""
    mp = pytest.MonkeyPatch()
    mp.setenv("PAYMENTS_URL", payments[1])
    endpoint = await create_nexus_endpoint(
        agents_client,
        name=ENDPOINT,
        target_namespace=AGENT_SERVICES_NAMESPACE,
        task_queue=FINANCE_TQ,
        description=render_markdown(),
    )
    finance = client_with_plugin(
        agent_services_client,
        scripted_plugin(ScriptedModel(finance_responder(finance_agents.FINANCE_PROMPT))),
    )
    support = client_with_plugin(
        agents_client, scripted_plugin(ScriptedModel(support_responder(PROMPTS)))
    )
    with ThreadPoolExecutor(max_workers=8) as executor:
        try:
            async with (
                build_finance_worker(finance, task_queue=FINANCE_TQ, activity_executor=executor),
                build_support_worker(support, task_queue=SUPPORT_TQ, activity_executor=executor),
            ):
                yield support, agent_services_client
        finally:
            await delete_nexus_endpoint(agents_client, endpoint)
            mp.undo()


async def run_case(
    support: Client, ticket: str, mode: str
) -> tuple[str, str, WorkflowHistory, dict[str, Any]]:
    wf_id = f"support-{mode}-{uuid.uuid4().hex[:8]}"
    handle = await support.start_workflow(
        SupportSupervisorWorkflow.run,
        args=[ticket, mode],
        id=wf_id,
        task_queue=SUPPORT_TQ,
        execution_timeout=timedelta(seconds=90),
    )
    reply = await handle.result()
    return (
        wf_id,
        reply,
        await handle.fetch_history(),
        await handle.query(SupportSupervisorWorkflow.a2a),
    )


@pytest.mark.parametrize("mode", ["tool", "handoff"])
async def test_case_a_supervisor_gets_a_refund_from_the_real_finance_agent(
    teams: tuple[Client, Client], payments: tuple[PaymentsServer, str], mode: str
) -> None:
    support, finance = teams
    wf_id, reply, history, a2a = await run_case(support, CASE_A, mode)

    # The reply quotes the refund Finance (not Support) issued through payments.
    (exchange,) = a2a["exchanges"]
    assert exchange["state"] == "completed"
    refund_id = exchange["refund_id"]
    assert refund_id and refund_id.startswith("R-") and refund_id in reply
    assert refund_id in {r.refund_id for r in payments[0].ledger.all()}

    # Caller history: discovery, the team, then the hop to the other namespace.
    steps = timeline(history)
    assert steps[0] == "nexus FinanceAgentA2A.get_agent_card"
    assert steps.count("nexus FinanceAgentA2A.send_message") == 1
    (send,) = [o for o in nexus_operations(history) if o.operation == "send_message"]
    assert send.links == (f"{AGENT_SERVICES_NAMESPACE}/a2a-task-{wf_id}",)
    assert send.outcome == "completed" and send.started
    expected_via = "ask_finance_agent tool" if mode == "tool" else "handoff"
    assert send.summary.endswith(f"via {expected_via}")

    # Finance's side: the A2A task Workflow in agent-services, not a child of Support.
    handle = finance.get_workflow_handle(f"a2a-task-{wf_id}")
    desc = await handle.describe()
    assert desc.status == WorkflowExecutionStatus.COMPLETED
    assert desc.parent_id is None
    task: A2ATask = await handle.query(FinanceAgentTaskWorkflow.get_task)
    assert task.status.state == "completed" and task.context_id == wf_id


async def approve_when_waiting(finance: Client, wf_id: str, verdict: str) -> None:
    """The human approver: wait for input-required, then signal (as approve.sh does)."""
    task_id = f"a2a-task-{wf_id}"
    await wait_for_a2a_task(finance, task_id, "input-required")
    signal = (
        FinanceAgentTaskWorkflow.approve
        if verdict == "approve"
        else FinanceAgentTaskWorkflow.reject
    )
    await finance.get_workflow_handle(task_id).signal(signal, ApproverDecision("dana.finance"))


@pytest.mark.parametrize("mode", ["tool", "handoff"])
async def test_case_b_waits_for_the_human_then_gets_the_refund(
    teams: tuple[Client, Client], payments: tuple[PaymentsServer, str], mode: str
) -> None:
    support, finance = teams
    wf_id = f"support-b-{mode}-{uuid.uuid4().hex[:8]}"
    handle = await support.start_workflow(
        SupportSupervisorWorkflow.run,
        args=[CASE_B, mode],
        id=wf_id,
        task_queue=SUPPORT_TQ,
        execution_timeout=timedelta(seconds=120),
    )
    await approve_when_waiting(finance, wf_id, "approve")
    reply = await handle.result()
    (exchange,) = (await handle.query(SupportSupervisorWorkflow.a2a))["exchanges"]
    assert exchange["state"] == "completed"
    assert exchange["refund_id"] in reply and "$450.00" in reply
    (refund,) = [r for r in payments[0].ledger.all() if r.idempotency_key == f"a2a-task-{wf_id}"]
    assert refund.amount_usd == 450.0
    task: A2ATask = await finance.get_workflow_handle(f"a2a-task-{wf_id}").query(
        FinanceAgentTaskWorkflow.get_task
    )
    assert "approved by Finance approver dana.finance" in (task.status.message.parts[0].text or "")  # type: ignore[union-attr]


async def test_case_b_rejected_by_the_human_gets_an_apology(teams: tuple[Client, Client]) -> None:
    support, finance = teams
    wf_id = f"support-b-reject-{uuid.uuid4().hex[:8]}"
    handle = await support.start_workflow(
        SupportSupervisorWorkflow.run,
        args=[CASE_B, "tool"],
        id=wf_id,
        task_queue=SUPPORT_TQ,
        execution_timeout=timedelta(seconds=120),
    )
    await approve_when_waiting(finance, wf_id, "reject")
    reply = await handle.result()
    (exchange,) = (await handle.query(SupportSupervisorWorkflow.a2a))["exchanges"]
    assert exchange["state"] == "rejected" and exchange["refund_id"] is None
    assert "sorry" in reply.lower() and "no refund was issued" in reply


async def test_support_worker_stopped_while_finance_waits(
    agents_client: Client, teams: tuple[Client, Client]
) -> None:
    """Plan v0.6: approve with no Support Worker running; a new Worker finishes the reply.

    While Finance waits, the Supervisor's Workflow has exactly one pending Nexus
    operation (``send_message``) and no Worker at all. The approval completes the
    Finance task, the server records ``NexusOperationCompleted`` in ``agents``,
    and the next Support Worker picks the conversation up from history.
    """
    _, finance = teams
    queue = f"support-stop-{uuid.uuid4().hex[:8]}"
    wf_id = f"support-b-stop-{uuid.uuid4().hex[:8]}"
    support = client_with_plugin(
        agents_client, scripted_plugin(ScriptedModel(support_responder(PROMPTS)))
    )
    with ThreadPoolExecutor(max_workers=4) as executor:
        async with build_support_worker(support, task_queue=queue, activity_executor=executor):
            handle = await support.start_workflow(
                SupportSupervisorWorkflow.run,
                args=[CASE_B, "tool"],
                id=wf_id,
                task_queue=queue,
                execution_timeout=timedelta(seconds=120),
            )
            await wait_for_a2a_task(finance, f"a2a-task-{wf_id}", "input-required")
        # The Support Worker is gone. One pending Nexus operation, still open.
        desc = await handle.describe()
        assert desc.status == WorkflowExecutionStatus.RUNNING
        (pending,) = desc.raw_description.pending_nexus_operations
        assert (pending.endpoint, pending.operation) == (ENDPOINT, "send_message")
        # The 25 h budget, capped by the server at this run's 120 s execution timeout:
        # an operation never outlives its caller (ledger 57; scripts start without one).
        assert pending.schedule_to_close_timeout.ToSeconds() == 120

        await approve_when_waiting(finance, wf_id, "approve")
        task = await finance.get_workflow_handle_for(
            FinanceAgentTaskWorkflow.run, f"a2a-task-{wf_id}"
        ).result()
        assert task.status.state == "completed"
        # Finance is done; Support has not moved (no Worker): its Workflow Task waits.
        await asyncio.sleep(1)
        history = await handle.fetch_history()
        send = [o for o in nexus_operations(history) if o.operation == "send_message"]
        assert send[0].outcome == "completed"
        assert (await handle.describe()).status == WorkflowExecutionStatus.RUNNING

        async with build_support_worker(support, task_queue=queue, activity_executor=executor):
            reply = await handle.result()
    assert task.artifacts and "R-" in reply and "$450.00" in reply
