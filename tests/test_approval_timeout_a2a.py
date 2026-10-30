"""v0.6: the end-to-end approval-timeout path, across namespaces (plan v0.6, ADR-0009).

``FINANCE_APPROVAL_TIMEOUT=5s`` on the real Finance Worker and
``SUPPORT_NEXUS_SEND_TIMEOUT=60s`` on the Support starter, both read through the
services' own ``config`` (the path the Workers and ``scripts/demo/case-b.sh``
use). Nobody approves Case B. Finance's wait ends first, so the Supervisor
receives an A2A task in state ``failed`` as the **result** of ``send_message``
(``NexusOperationCompleted``, not a ``NexusOperationError``) and replies with an
apology and an escalation.

Its own module (own endpoint and Finance Worker) because the endpoint name
``finance-agent`` is unique per server and the other modules' Finance Worker
waits the default 24 h. Shape follows ``tests/test_supervisor_a2a.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
import pytest_asyncio
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client

from demo_testing import AGENT_SERVICES_NAMESPACE
from demo_testing.finance_script import finance_responder
from demo_testing.history import nexus_operations
from demo_testing.nexus import create_nexus_endpoint, delete_nexus_endpoint
from demo_testing.scripted_model import ScriptedModel, client_with_plugin, scripted_plugin
from demo_testing.support_script import SupportPrompts, support_responder
from finance_a2a_api import ENDPOINT
from finance_agent import agents as finance_agents
from finance_agent import config as finance_config
from finance_agent.worker import build_worker as build_finance_worker
from finance_agent.workflows import FinanceAgentTaskWorkflow
from payments_mock.server import PaymentsServer, serve_in_thread
from support_agent import agents
from support_agent import config as support_config
from support_agent.worker_factory import build_worker as build_support_worker
from support_agent.workflows import SupportSupervisorWorkflow

pytestmark = [
    pytest.mark.integration,
    pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning"),
]

FINANCE_TQ = f"agent-services-tq-timeout-{uuid.uuid4().hex[:8]}"
SUPPORT_TQ = f"support-tq-timeout-{uuid.uuid4().hex[:8]}"
PROMPTS = SupportPrompts(
    agents.SUPERVISOR_PROMPT,
    agents.ORDER_LOOKUP_PROMPT,
    agents.RETURN_POLICY_PROMPT,
    agents.SUPERVISOR_HANDOFF_PROMPT,
)
CASE_B = "Order #2077: my laptop arrived with a cracked screen. I want a refund."


@pytest.fixture(scope="module")
def short_timeouts() -> Iterator[None]:
    mp = pytest.MonkeyPatch()
    mp.setenv("FINANCE_APPROVAL_TIMEOUT", "5s")
    mp.setenv("SUPPORT_NEXUS_SEND_TIMEOUT", "60s")
    try:
        yield
    finally:
        mp.undo()


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
    short_timeouts: None,
    agents_client: Client,
    agent_services_client: Client,
    payments: tuple[PaymentsServer, str],
) -> AsyncIterator[tuple[Client, Client]]:
    mp = pytest.MonkeyPatch()
    mp.setenv("PAYMENTS_URL", payments[1])
    endpoint = await create_nexus_endpoint(
        agents_client,
        name=ENDPOINT,
        target_namespace=AGENT_SERVICES_NAMESPACE,
        task_queue=FINANCE_TQ,
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
                # approval_timeout left to the Worker's config: FINANCE_APPROVAL_TIMEOUT=5s.
                build_finance_worker(finance, task_queue=FINANCE_TQ, activity_executor=executor),
                build_support_worker(support, task_queue=SUPPORT_TQ, activity_executor=executor),
            ):
                yield support, agent_services_client
        finally:
            await delete_nexus_endpoint(agents_client, endpoint)
            mp.undo()


async def test_nobody_approves_supervisor_gets_failed_task_and_escalates(
    teams: tuple[Client, Client], payments: tuple[PaymentsServer, str]
) -> None:
    support, finance = teams
    budget = support_config.nexus_send_timeout()
    assert (budget, finance_config.approval_timeout()) == ("60s", "5s")
    wf_id = f"support-timeout-{uuid.uuid4().hex[:8]}"
    handle = await support.start_workflow(
        SupportSupervisorWorkflow.run,
        args=[CASE_B, "tool", budget],
        id=wf_id,
        task_queue=SUPPORT_TQ,
        execution_timeout=timedelta(seconds=120),
    )
    reply = await handle.result()

    # Support: the operation COMPLETED with an A2A task in state failed.
    history = await handle.fetch_history()
    (send,) = [o for o in nexus_operations(history) if o.operation == "send_message"]
    assert send.outcome == "completed", "an A2A answer, not a NexusOperationError"
    scheduled = [
        e.nexus_operation_scheduled_event_attributes
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_NEXUS_OPERATION_SCHEDULED
        and e.nexus_operation_scheduled_event_attributes.operation == "send_message"
    ]
    assert scheduled[0].schedule_to_close_timeout.ToSeconds() == 60
    (exchange,) = (await handle.query(SupportSupervisorWorkflow.a2a))["exchanges"]
    assert exchange["state"] == "failed" and exchange["refund_id"] is None
    assert "No Finance approver decided within 5s" in exchange["reply"]
    assert "sorry" in reply.lower() and "escalated" in reply

    # Finance: the task Workflow completed normally with the failed A2A task; no money moved.
    task = await finance.get_workflow_handle_for(
        FinanceAgentTaskWorkflow.run, f"a2a-task-{wf_id}"
    ).result()
    assert task.status.state == "failed"
    assert not [r for r in payments[0].ledger.all() if r.idempotency_key == task.id]
