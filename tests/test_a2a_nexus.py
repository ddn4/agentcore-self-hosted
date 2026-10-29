"""The A2A hop over Nexus, across namespaces, on the two-namespace dev server.

Plan §5 "Nexus (2 namespaces)" layer (v0.4 acceptance). Nexus does not run on
the time-skipping test server (samples-python skips its Nexus tests there;
``spikes/nexus_time_skipping.py``), so this uses the shared ``start_local``
dev server with both namespaces:

- caller: ``A2ASmokeWorkflow`` (no LLM, default converter) in ``agents``;
- handler: the Finance Worker exactly as ``finance_agent.worker`` builds it
  (StrandsPlugin with a ScriptedModel under ``"bedrock"``, the
  ``FinanceAgentA2A`` handler) in ``agent-services``;
- the Nexus endpoint ``finance-agent`` (the real name, with the Agent Card as
  its description) routing to a test-unique task queue;
- a real payments-mock on an ephemeral port.

Shape follows samples-python ``tests/hello_nexus/hello_nexus_test.py``
(endpoint created per test module and deleted afterwards).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
import pytest_asyncio
from temporalio.api.enums.v1 import EventType
from temporalio.api.history.v1 import HistoryEvent
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowHistory
from temporalio.worker import Worker

from demo_testing import AGENT_SERVICES_NAMESPACE
from demo_testing.finance_script import finance_responder
from demo_testing.history import nexus_operations
from demo_testing.nexus import create_nexus_endpoint, delete_nexus_endpoint, endpoint_description
from demo_testing.nexus_probes import DoubleSendWorkflow
from demo_testing.scripted_model import ScriptedModel, client_with_plugin, scripted_plugin
from finance_a2a_api import (
    ENDPOINT,
    FINANCE_AGENT_CARD,
    SERVICE,
    A2ATask,
    AgentCardRequest,
    FinanceAgentA2A,
    first_data,
    render_markdown,
    status_text,
)
from finance_agent import agents
from finance_agent.worker import build_worker
from finance_agent.workflows import FinanceAgentTaskWorkflow
from payments_mock.server import PaymentsServer, serve_in_thread
from support_agent.smoke import A2ASmokeWorkflow
from support_agent.smoke_worker import build_smoke_worker

pytestmark = pytest.mark.integration

FINANCE_TQ = f"agent-services-tq-test-{uuid.uuid4().hex[:8]}"
SMOKE_TQ = f"nexus-smoke-tq-test-{uuid.uuid4().hex[:8]}"
PROBE_TQ = f"nexus-probe-tq-test-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def payments() -> Iterator[tuple[PaymentsServer, str]]:
    server, url = serve_in_thread()
    try:
        yield server, url
    finally:
        server.shutdown()
        server.server_close()


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def a2a(
    agents_client: Client,
    agent_services_client: Client,
    payments: tuple[PaymentsServer, str],
) -> AsyncIterator[tuple[Client, Client]]:
    """Endpoint + Finance Worker + smoke Worker for this module; yields both clients."""
    mp = pytest.MonkeyPatch()
    mp.setenv("PAYMENTS_URL", payments[1])
    endpoint = await create_nexus_endpoint(
        agents_client,
        name=ENDPOINT,
        target_namespace=AGENT_SERVICES_NAMESPACE,
        task_queue=FINANCE_TQ,
        description=render_markdown(),
    )
    finance_client = client_with_plugin(
        agent_services_client,
        scripted_plugin(ScriptedModel(finance_responder(agents.FINANCE_PROMPT))),
    )
    with ThreadPoolExecutor(max_workers=4) as executor:
        try:
            async with (
                build_worker(finance_client, task_queue=FINANCE_TQ, activity_executor=executor),
                build_smoke_worker(agents_client, task_queue=SMOKE_TQ),
                Worker(agents_client, task_queue=PROBE_TQ, workflows=[DoubleSendWorkflow]),
            ):
                yield agents_client, agent_services_client
        finally:
            await delete_nexus_endpoint(agents_client, endpoint)
            mp.undo()


def _events(history: WorkflowHistory, kind: EventType.ValueType) -> list[HistoryEvent]:
    return [e for e in history.events if e.event_type == kind]


def _summary(event: HistoryEvent) -> str:
    return json.loads(event.user_metadata.summary.data) if event.user_metadata.summary.data else ""


async def run_smoke(caller: Client, amount: float, order_id: str) -> tuple[str, A2ATask]:
    wf_id = f"smoke-{order_id}-{uuid.uuid4().hex[:8]}"
    task = await caller.execute_workflow(
        A2ASmokeWorkflow.run,
        args=[amount, order_id],
        id=wf_id,
        task_queue=SMOKE_TQ,
        execution_timeout=timedelta(seconds=60),
    )
    return wf_id, task


async def test_endpoint_publishes_the_agent_card(a2a: tuple[Client, Client]) -> None:
    caller, _ = a2a
    from temporalio.api.operatorservice.v1 import ListNexusEndpointsRequest

    resp = await caller.operator_service.list_nexus_endpoints(
        ListNexusEndpointsRequest(name=ENDPOINT)
    )
    (endpoint,) = resp.endpoints
    assert endpoint.spec.target.worker.namespace == AGENT_SERVICES_NAMESPACE
    assert endpoint_description(endpoint) == render_markdown()


async def test_smoke_1042_crosses_namespaces_and_completes(
    a2a: tuple[Client, Client], payments: tuple[PaymentsServer, str]
) -> None:
    caller, handler = a2a
    wf_id, task = await run_smoke(caller, 40.0, "1042")

    # The A2A task: completed, with the refund_confirmation artifact.
    assert task.id == f"a2a-task-{wf_id}" and task.context_id == wf_id
    assert task.status.state == "completed", status_text(task)
    (artifact,) = task.artifacts
    confirmation = first_data(artifact.parts)
    assert artifact.name == "refund_confirmation" and confirmation is not None
    assert confirmation["refund_id"] in {r.refund_id for r in payments[0].ledger.all()}

    # Caller history: two Nexus operations on endpoint finance-agent, each with a summary.
    history = await caller.get_workflow_handle(wf_id).fetch_history()
    scheduled = _events(history, EventType.EVENT_TYPE_NEXUS_OPERATION_SCHEDULED)
    assert [
        (
            e.nexus_operation_scheduled_event_attributes.endpoint,
            e.nexus_operation_scheduled_event_attributes.service,
            e.nexus_operation_scheduled_event_attributes.operation,
        )
        for e in scheduled
    ] == [(ENDPOINT, SERVICE, "get_agent_card"), (ENDPOINT, SERVICE, "send_message")]
    summaries = [_summary(e) for e in scheduled]
    assert summaries[0].startswith("A2A → Finance Refund Agent: get_agent_card")
    assert summaries[1].startswith("A2A → Finance Refund Agent: send_message")
    # Sync operation: Scheduled -> Completed. Async: Scheduled -> Started -> Completed.
    (started,) = _events(history, EventType.EVENT_TYPE_NEXUS_OPERATION_STARTED)
    assert len(_events(history, EventType.EVENT_TYPE_NEXUS_OPERATION_COMPLETED)) == 2
    # The Started event links to the Finance Workflow in the OTHER namespace.
    links = [link.workflow_event for link in started.links if link.HasField("workflow_event")]
    assert [(link.namespace, link.workflow_id) for link in links] == [
        (AGENT_SERVICES_NAMESPACE, task.id)
    ]

    # Handler side: the task Workflow in agent-services links back to the caller.
    handle = handler.get_workflow_handle(task.id)
    desc = await handle.describe()
    assert desc.status == WorkflowExecutionStatus.COMPLETED
    static_summary = await desc.static_summary()
    assert static_summary is not None and "refund order 1042 $40.00" in static_summary
    finance_history = await handle.fetch_history()
    started_attrs = finance_history.events[0].workflow_execution_started_event_attributes
    # Server 1.32 keeps the back-link on the completion callback (URL temporal://system).
    (callback,) = started_attrs.completion_callbacks
    assert callback.nexus.url == "temporal://system"
    back = [link.workflow_event for link in callback.links if link.HasField("workflow_event")]
    assert [(link.namespace, link.workflow_id) for link in back] == [("agents", wf_id)]
    # Finished Workflows still answer queries (replayed by the Worker).
    assert (await handle.query(FinanceAgentTaskWorkflow.get_task)) == task


async def test_smoke_2077_is_rejected_by_the_limit(a2a: tuple[Client, Client]) -> None:
    caller, _ = a2a
    _, task = await run_smoke(caller, 450.0, "2077")
    assert task.status.state == "rejected"
    assert "needs a Finance approver" in status_text(task)


async def test_same_context_sent_twice_runs_one_task(
    a2a: tuple[Client, Client], payments: tuple[PaymentsServer, str]
) -> None:
    caller, handler = a2a
    context_id = f"support-dup-{uuid.uuid4().hex[:8]}"
    refunds_before = len(payments[0].ledger.all())
    tasks = await caller.execute_workflow(
        DoubleSendWorkflow.run,
        args=[ENDPOINT, context_id],
        id=f"double-{context_id}",
        task_queue=PROBE_TQ,
        execution_timeout=timedelta(seconds=60),
    )
    assert len(tasks) == 2
    assert tasks[0] == tasks[1]
    assert tasks[0].id == f"a2a-task-{context_id}" and tasks[0].status.state == "completed"
    runs = [wf async for wf in handler.list_workflows(f"WorkflowId = 'a2a-task-{context_id}'")]
    assert len(runs) == 1, "one Finance Workflow for one conversation"
    assert len(payments[0].ledger.all()) == refunds_before + 1, "one refund"


async def test_standalone_get_agent_card_from_a_client(a2a: tuple[Client, Client]) -> None:
    """Q16 (SDK side): a Standalone Nexus Operation, no caller Workflow at all."""
    caller, _ = a2a
    nexus_client = caller.create_nexus_client(service=FinanceAgentA2A, endpoint=ENDPOINT)
    card = await nexus_client.execute_operation(
        FinanceAgentA2A.get_agent_card,
        AgentCardRequest(),
        id=f"card-{uuid.uuid4().hex[:8]}",
        schedule_to_close_timeout=timedelta(seconds=30),
    )
    assert card == FINANCE_AGENT_CARD


async def test_resend_after_completion_moves_no_money_twice(
    a2a: tuple[Client, Client], payments: tuple[PaymentsServer, str]
) -> None:
    """A closed task's conversation sent again: a new run, the same refund (payments replay)."""
    caller, handler = a2a
    wf_id = f"smoke-resend-{uuid.uuid4().hex[:8]}"
    tasks = []
    for _ in range(2):
        tasks.append(
            await caller.execute_workflow(
                A2ASmokeWorkflow.run,
                args=[40.0, "1042"],
                id=wf_id,
                task_queue=SMOKE_TQ,
                execution_timeout=timedelta(seconds=60),
            )
        )
    refund_ids = [first_data(t.artifacts[0].parts)["refund_id"] for t in tasks]  # type: ignore[index]
    assert refund_ids[0] == refund_ids[1], "payments replayed the first refund"
    assert [r.refund_id for r in payments[0].ledger.all()].count(refund_ids[0]) == 1
    runs = [wf async for wf in handler.list_workflows(f"WorkflowId = 'a2a-task-{wf_id}'")]
    assert len(runs) == 2, "a closed task's resend is a new run of the same Workflow ID"


async def test_history_helper_reads_the_hop(a2a: tuple[Client, Client]) -> None:
    caller, _ = a2a
    wf_id, task = await run_smoke(caller, 40.0, "1042")
    ops = nexus_operations(await caller.get_workflow_handle(wf_id).fetch_history())
    assert [(o.operation, o.outcome, o.started) for o in ops] == [
        ("get_agent_card", "completed", False),
        ("send_message", "completed", True),
    ]
    assert ops[1].links == (f"{AGENT_SERVICES_NAMESPACE}/{task.id}",)
