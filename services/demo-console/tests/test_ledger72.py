"""Ledger 72: the console never queries a Support Workflow (DESIGN.md 4.3).

A query on a SupportSupervisorWorkflow on support-tq makes the WCI start an AgentCore session
and hangs when nothing polls. Every RunView and every route is built here
against FakeGateway, which records each call: none may be a query in namespace ``agents``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import cast

import httpx
import pytest
from temporalio.client import Client, WorkflowHistory

from demo_console.app import create_app
from demo_console.clients import AgentsClient, Execution, SupportQueryForbidden
from demo_console.fakes import FakeGateway, FakeHttp, FakeRunner, make_settings

pytestmark = pytest.mark.unit
Load = Callable[..., WorkflowHistory]
HEADERS = {"X-Console": "1", "Content-Type": "application/json"}


async def test_agents_client_has_no_query_path() -> None:
    agents = AgentsClient(cast(Client, object()))  # never touched: the guard raises first
    with pytest.raises(SupportQueryForbidden):
        await agents.query("support-2077", "a2a")


async def test_every_route_without_a_support_query(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp, load: Load
) -> None:
    runs = {
        "support-1042-ca01": "case-a",
        "support-2077-cb01": "case-b-approve",
        "support-2077-sl-s1": "serverless-b",
        "support-2077-wci-w1": "wci-b",
        "support-1042-crash-k1": "crash",
    }
    for wf, name in runs.items():
        gateway.add_run(wf, load(f"{name}.support"), load(f"{name}.finance"))
    gateway.executions["agents|WorkflowType='SupportSupervisorWorkflow'"] = [
        Execution(
            wf, "run-1", "SupportSupervisorWorkflow", "COMPLETED", "support-dev-tq", None, None
        )
        for wf in runs
    ]
    app = create_app(
        make_settings(tmp_path), clients=gateway, runner=runner, http=http, static_dir=tmp_path
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8099") as client:
        for path in (
            "/api/config",
            "/api/stack",
            "/api/scenarios",
            "/api/runs",
            "/api/actions",
            "/api/processes",
            "/api/jobs",
        ):
            assert (await client.get(path)).status_code == 200, path
        for wf in runs:
            assert (await client.get(f"/api/runs/{wf}")).status_code == 200
            assert (await client.get(f"/api/runs/{wf}/rows")).status_code == 200
            assert (await client.get(f"/api/runs/{wf}/report")).status_code == 200
        # approve on a closed task (409) and a start: still no Support query
        resp = await client.post(
            "/api/runs/support-2077-cb01/approve",
            json={"approver": "dana.finance"},
            headers=HEADERS,
        )
        assert resp.status_code == 409
        resp = await client.post("/api/runs", json={"scenario": "case-a"}, headers=HEADERS)
        assert resp.status_code == 201
    assert gateway.support_queries() == []
    # Finance was queried or read by history, which is allowed.
    assert any(c[1] == "agent-services" for c in gateway.calls)


async def test_clients_route_agents_queries_to_the_guard(tmp_path: Path) -> None:
    from demo_console.clients import Clients

    clients = Clients.from_clients(
        make_settings(tmp_path), cast(Client, object()), cast(Client, object())
    )
    with pytest.raises(SupportQueryForbidden):
        await clients.query("agents", "support-2077", "a2a")
    with pytest.raises(ValueError, match="only signals Finance"):
        await clients.signal("agents", "support-2077", "approve", {})
