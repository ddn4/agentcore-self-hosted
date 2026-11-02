"""The console against a real two-namespace dev server (DESIGN.md 6.2); no AWS.

Scripted Support and Finance Workers run in-process (the pattern of
``tests/test_supervisor_a2a.py``), behind the real endpoint name ``finance-agent``. The console
app is built with ``create_app(clients=...)`` and driven through ``httpx.ASGITransport``;
a spy on the client records every query, and none may target namespace ``agents``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from temporalio.client import Client

from demo_console.app import create_app
from demo_console.clients import Clients
from demo_console.fakes import FakeHttp, FakeRunner, make_settings
from demo_testing import AGENT_SERVICES_NAMESPACE
from demo_testing.finance_script import finance_responder
from demo_testing.nexus import create_nexus_endpoint, delete_nexus_endpoint
from demo_testing.scripted_model import ScriptedModel, client_with_plugin, scripted_plugin
from demo_testing.support_script import SupportPrompts, support_responder
from finance_a2a_api import ENDPOINT, render_markdown
from finance_agent import agents as finance_agents
from finance_agent.worker import build_worker as build_finance_worker
from payments_mock.server import PaymentsServer, serve_in_thread
from support_agent import agents
from support_agent.worker_factory import build_worker as build_support_worker

pytestmark = [
    pytest.mark.integration,
    pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning"),
]

FINANCE_TQ = f"agent-services-tq-console-{uuid.uuid4().hex[:8]}"
SUPPORT_TQ = "support-dev-tq"  # the console starts Case A/B there (plain shape)
HEADERS = {"X-Console": "1", "Content-Type": "application/json"}
PROMPTS = SupportPrompts(
    agents.SUPERVISOR_PROMPT,
    agents.ORDER_LOOKUP_PROMPT,
    agents.RETURN_POLICY_PROMPT,
    agents.SUPERVISOR_HANDOFF_PROMPT,
)


@pytest.fixture(scope="module")
def payments() -> Iterator[tuple[PaymentsServer, str]]:
    server, url = serve_in_thread()
    try:
        yield server, url
    finally:
        server.shutdown()
        server.server_close()


class SpyClients(Clients):
    """Real clients; every query is recorded before the ledger-72 guard sees it."""

    queried: list[tuple[str, str, str]]

    async def query(self, namespace: str, workflow_id: str, name: str, within: float = 5.0) -> Any:
        self.queried.append((namespace, workflow_id, name))
        return await super().query(namespace, workflow_id, name, within)


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def console_api(
    agents_client: Client,
    agent_services_client: Client,
    payments: tuple[PaymentsServer, str],
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncIterator[tuple[httpx.AsyncClient, SpyClients]]:
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
    root: Path = tmp_path_factory.mktemp("console")
    settings = make_settings(root)
    spy = SpyClients(settings, agents=agents_client, services=agent_services_client)
    spy.queried = []
    app = create_app(settings, clients=spy, runner=FakeRunner(), http=FakeHttp(), static_dir=root)
    with ThreadPoolExecutor(max_workers=8) as executor:
        try:
            async with (
                build_finance_worker(finance, task_queue=FINANCE_TQ, activity_executor=executor),
                build_support_worker(support, task_queue=SUPPORT_TQ, activity_executor=executor),
            ):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://127.0.0.1:8099", timeout=30
                ) as client:
                    yield client, spy
        finally:
            await delete_nexus_endpoint(agents_client, endpoint)
            mp.undo()


async def wait_for(
    client: httpx.AsyncClient, wf: str, check: Any, within: float = 60
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + within
    view: dict[str, Any] = {}
    while asyncio.get_running_loop().time() < deadline:
        resp = await client.get(f"/api/runs/{wf}")
        if resp.status_code == 200:
            view = resp.json()
            if check(view):
                return view
        await asyncio.sleep(0.3)
    raise AssertionError(
        f"{wf}: condition not reached; last view: {view.get('status')} {view.get('task')}"
    )


async def start(client: httpx.AsyncClient, scenario: str) -> str:
    wf = f"support-console-{scenario}-{uuid.uuid4().hex[:6]}"
    resp = await client.post(
        "/api/runs", json={"scenario": scenario, "workflow_id": wf, "queue": "dev"}, headers=HEADERS
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["attached"] is False
    return wf


async def test_case_a_through_the_console(
    console_api: tuple[httpx.AsyncClient, SpyClients],
) -> None:
    client, spy = console_api
    wf = await start(client, "case-a")
    view = await wait_for(client, wf, lambda v: v["status"] == "COMPLETED")
    assert view["scenario"] == "case-a"
    assert view["task"]["artifact"]["approved_by"] == "auto"
    refund_id = view["task"]["artifact"]["refund_id"]
    assert view["reply"]["refund_id"] == refund_id and refund_id in view["reply"]["text"]
    assert view["task"]["state"] == "completed" and view["support"]["pending_nexus"] == []
    assert view["metrics"]["held_open_s"] is not None and view["metrics"]["held_open_s"] > 0
    assert view["links"]["finance"].endswith(f"/namespaces/agent-services/workflows/a2a-task-{wf}")
    rows = (await client.get(f"/api/runs/{wf}/rows")).json()["rows"]
    out = [r for r in rows if r.get("pair") and r["kind"] in ("nexus.scheduled", "task.started")]
    pairs = {r["pair"] for r in out if r["kind"] == "task.started"}
    assert any(r["kind"] == "nexus.scheduled" and r["pair"] in pairs for r in out)
    # The hop, both ways: send_message leaves Support for Finance's lane, and Finance's
    # completed task comes back over the seam as the Nexus completion in Support's history.
    sends = [r for r in rows if r["kind"] == "nexus.scheduled" and r.get("pair") in pairs]
    assert [r["operation"] for r in sends] == ["send_message"]
    assert sends[0]["from"] == "support" and sends[0]["to"] == "finance"
    back = {
        r["kind"]: r
        for r in rows
        if r.get("pair") == f"{sends[0]['pair']}-back"
        and r["kind"] in ("task.state", "nexus.completed")
    }
    assert back["task.state"]["lane"] == "finance" and back["task.state"]["state"] == "completed"
    assert back["nexus.completed"]["lane"] == "nexus"
    assert back["nexus.completed"]["operation"] == "send_message"
    assert any(r["kind"] == "nexus.completed" and r["operation"] == "get_agent_card" for r in rows)
    assert [r["kind"] for r in rows][-1] == "run.completed"
    assert all(s["status"] == "done" for s in view["steps"])
    assert [q for q in spy.queried if q[0] == "agents"] == []


async def test_case_b_approve_then_reject_and_409(
    console_api: tuple[httpx.AsyncClient, SpyClients],
) -> None:
    client, spy = console_api
    approve_wf = await start(client, "case-b")
    reject_wf = await start(client, "case-b")
    for wf in (approve_wf, reject_wf):
        await wait_for(client, wf, lambda v: v["task"]["state"] == "input-required")

    resp = await client.post(
        f"/api/runs/{approve_wf}/approve",
        json={"approver": "dana.finance", "comment": "ok"},
        headers=HEADERS,
    )
    assert resp.status_code == 200 and resp.json()["state_after"] == "completed"
    view = await wait_for(client, approve_wf, lambda v: v["status"] == "COMPLETED")
    assert view["task"]["artifact"]["approved_by"] == "dana.finance"
    report = (await client.get(f"/api/runs/{approve_wf}/report")).json()
    assert report["finance"]["signal"]["approver"] == "dana.finance"
    assert report["support_during_wait"]["workflow_tasks"] >= 0

    resp = await client.post(
        f"/api/runs/{reject_wf}/reject", json={"approver": "dana.finance"}, headers=HEADERS
    )
    assert resp.status_code == 200 and resp.json()["state_after"] == "rejected"
    view = await wait_for(client, reject_wf, lambda v: v["status"] == "COMPLETED")
    assert view["task"]["state"] == "rejected" and view["reply"]["refund_id"] is None

    again = await client.post(
        f"/api/runs/{approve_wf}/approve", json={"approver": "x"}, headers=HEADERS
    )
    assert again.status_code == 409 and again.json()["error"]["code"] == "not_input_required"

    listing = (await client.get("/api/runs")).json()
    ours = {r["workflow_id"]: r for r in listing["runs"]}
    assert ours[reject_wf]["task_state"] == "rejected"
    assert [q for q in spy.queried if q[0] == "agents"] == []
