"""HTTP routes (DESIGN.md 5): guard, error shapes, approve/reject, starting a run."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowHistory

from demo_console.app import Console, create_app
from demo_console.fakes import FakeGateway, FakeHttp, FakeRunner, make_settings, task_value, until

pytestmark = pytest.mark.unit
Load = Callable[..., WorkflowHistory]
HEADERS = {"X-Console": "1", "Content-Type": "application/json"}
E = EventType


@pytest_asyncio.fixture
async def api(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp
) -> AsyncIterator[tuple[httpx.AsyncClient, Console]]:
    app = create_app(
        make_settings(tmp_path),
        clients=gateway,
        runner=runner,
        http=http,
        static_dir=tmp_path,
        settle_s=0.5,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8099") as client:
        yield client, app.state.console


def error(resp: httpx.Response) -> dict[str, Any]:
    body = resp.json()
    assert set(body) == {"error"} and {"code", "message", "hint"} <= set(body["error"])
    return body["error"]


async def test_guard(api: tuple[httpx.AsyncClient, Console]) -> None:
    client, _ = api
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    assert (await client.get("/healthz", headers={"Host": "localhost:8099"})).status_code == 200
    bad = await client.get("/api/config", headers={"Host": "attacker.example:8099"})
    assert bad.status_code == 403 and error(bad)["code"] == "forbidden_origin"
    no_header = await client.post("/api/runs", json={"scenario": "case-a"})
    assert no_header.status_code == 403 and error(no_header)["code"] == "forbidden_origin"
    form = await client.post(
        "/api/runs",
        content="scenario=case-a",
        headers={"X-Console": "1", "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert form.status_code == 403
    bad_json = await client.post("/api/runs", content="{", headers=HEADERS)
    assert bad_json.status_code == 400 and error(bad_json)["code"] == "bad_request"


async def test_start_run_matches_case_a_sh(
    api: tuple[httpx.AsyncClient, Console], gateway: FakeGateway
) -> None:
    client, c = api
    resp = await client.post(
        "/api/runs",
        json={"scenario": "case-b", "workflow_id": "support-2077-t1", "a2a_mode": "handoff"},
        headers=HEADERS,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body == {
        "workflow_id": "support-2077-t1",
        "run_id": "run-new",
        "attached": False,
        "links": {
            "support": "http://127.0.0.1:8233/namespaces/agents/workflows/support-2077-t1",
            "finance": "http://127.0.0.1:8233/namespaces/agent-services/workflows/a2a-task-support-2077-t1",
        },
    }
    start = gateway.starts[-1]
    assert start == {
        "namespace": "agents",
        "type": "SupportSupervisorWorkflow",
        "args": [
            "Order #2077: my laptop arrived with a cracked screen. I want a refund.",
            "handoff",
            "25h",
        ],
        "workflow_id": "support-2077-t1",
        "task_queue": "support-dev-tq",
    }
    assert c.runs.record("support-2077-t1")["scenario"] == "case-b"
    # Default ID and the versioned queue.
    resp = await client.post("/api/runs", json={"scenario": "wci-b"}, headers=HEADERS)
    assert resp.json()["workflow_id"].startswith("support-2077-wci-")
    assert gateway.starts[-1]["task_queue"] == "support-tq"
    assert gateway.starts[-1]["args"][0].startswith("Order #2077")


async def test_start_run_errors(api: tuple[httpx.AsyncClient, Console]) -> None:
    client, c = api
    cases = [
        ({"scenario": "nope"}, 404, "not_found"),
        ({"scenario": "smoke"}, 422, "invalid_param"),
        ({"scenario": "case-a", "a2a_mode": "shout"}, 422, "invalid_param"),
        ({"scenario": "case-a", "nexus_send_timeout": "soon"}, 422, "invalid_param"),
        ({"scenario": "case-a", "workflow_id": "a2a-task-x"}, 422, "invalid_param"),
        ({"scenario": "case-a", "workflow_id": "bad id/with slash"}, 422, "invalid_param"),
    ]
    for body, status, code in cases:
        resp = await client.post("/api/runs", json=body, headers=HEADERS)
        assert resp.status_code == status, body
        assert error(resp)["code"] == code
    # A live model costs tokens: confirm first.
    c.stack.last = {"model": {"mode": "live"}}
    c.hub.stack_cache._value = c.stack.last
    c.hub.stack_cache._at = 1e18
    resp = await client.post("/api/runs", json={"scenario": "case-a"}, headers=HEADERS)
    assert resp.status_code == 409 and error(resp)["code"] == "confirm_required"
    resp = await client.post(
        "/api/runs", json={"scenario": "case-a", "confirm": True}, headers=HEADERS
    )
    assert resp.status_code == 201


async def test_approve_paths(
    api: tuple[httpx.AsyncClient, Console], gateway: FakeGateway, load: Load
) -> None:
    client, _ = api
    wf = "support-2077-cb01"
    sup = until(load("case-b-approve.support"), E.EVENT_TYPE_NEXUS_OPERATION_STARTED, 1)
    fin = until(load("case-b-approve.finance"), E.EVENT_TYPE_TIMER_STARTED)
    states = iter(["input-required", "working", "completed"])
    gateway.add_run(wf, sup, fin, task=lambda: task_value(next(states)))

    empty = await client.post(f"/api/runs/{wf}/approve", json={"approver": "  "}, headers=HEADERS)
    assert empty.status_code == 422 and error(empty)["code"] == "invalid_param"
    ok = await client.post(
        f"/api/runs/{wf}/approve",
        json={"approver": "dana.finance", "comment": "ok"},
        headers=HEADERS,
    )
    assert ok.status_code == 200
    assert ok.json() == {
        "workflow_id": wf,
        "task_id": f"a2a-task-{wf}",
        "verdict": "approve",
        "state_before": "input-required",
        "state_after": "completed",
        "settled": True,
    }
    assert gateway.signals == [
        (
            "agent-services",
            f"a2a-task-{wf}",
            "approve",
            {"approver": "dana.finance", "comment": "ok"},
        )
    ]
    # Now completed: 409 with the state in the message.
    gateway.queries[("agent-services", f"a2a-task-{wf}", "get_task")] = task_value("completed")
    again = await client.post(
        f"/api/runs/a2a-task-{wf}/reject", json={"approver": "x"}, headers=HEADERS
    )
    assert again.status_code == 409
    err = error(again)
    assert err["code"] == "not_input_required" and "completed" in err["message"]
    assert err["hint"] == "Open the run to see who decided."
    missing = await client.post("/api/runs/support-9/approve", json={}, headers=HEADERS)
    assert missing.status_code == 404


async def test_reject_without_finance_poller_uses_history(
    api: tuple[httpx.AsyncClient, Console], gateway: FakeGateway, load: Load
) -> None:
    client, _ = api
    wf = "support-2077-cb01"
    sup = until(load("case-b-approve.support"), E.EVENT_TYPE_NEXUS_OPERATION_STARTED, 1)
    fin = until(load("case-b-approve.finance"), E.EVENT_TYPE_TIMER_STARTED)
    gateway.add_run(wf, sup, fin)  # no get_task answer: the query times out
    resp = await client.post(
        f"/api/runs/{wf}/reject", json={"approver": "dana.finance"}, headers=HEADERS
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["state_before"] == "input-required" and body["settled"] is False
    assert gateway.signals[-1][2] == "reject"


async def test_terminate_needs_confirm(
    api: tuple[httpx.AsyncClient, Console], gateway: FakeGateway, load: Load
) -> None:
    client, _ = api
    wf = "support-2077-cb01"
    sup = until(load("case-b-approve.support"), E.EVENT_TYPE_NEXUS_OPERATION_STARTED, 1)
    fin = until(load("case-b-approve.finance"), E.EVENT_TYPE_TIMER_STARTED)
    gateway.add_run(wf, sup, fin)
    resp = await client.post(f"/api/runs/{wf}/terminate", json={}, headers=HEADERS)
    assert resp.status_code == 409 and error(resp)["code"] == "confirm_required"
    resp = await client.post(
        f"/api/runs/{wf}/terminate", json={"confirm": True, "reason": "leftover"}, headers=HEADERS
    )
    assert resp.json() == {"terminated": True, "finance_terminated": True}
    assert gateway.terminated == [
        ("agents", wf, "leftover"),
        ("agent-services", f"a2a-task-{wf}", "leftover"),
    ]


async def test_reads_and_unreachable(
    api: tuple[httpx.AsyncClient, Console], gateway: FakeGateway
) -> None:
    client, c = api
    assert (await client.get("/api/runs/nope")).status_code == 404
    gateway.up = False
    c.hub.stack_cache.invalidate()
    c.hub.runs_cache.invalidate()
    for path in ("/api/runs", "/api/runs/whatever", "/api/runs/whatever/report"):
        resp = await client.get(path)
        assert resp.status_code == 503, path
        err = error(resp)
        assert err["code"] == "temporal_unreachable"
        assert err["hint"] == "Start the dev server (Manage stack), or run make dev-server-up."
    stack = (await client.get("/api/stack")).json()
    assert stack["components"][0]["status"] == "down"
    assert (await client.get("/api/jobs/missing")).status_code == 404
    assert (await client.get("/api/processes/nope/log")).status_code == 404
    log = await client.get("/api/processes/worker-support/log?raw=1")
    assert log.status_code == 200 and log.headers["content-type"].startswith("text/plain")


async def test_profile_switch(api: tuple[httpx.AsyncClient, Console], runner: FakeRunner) -> None:
    client, c = api
    bad = await client.post("/api/profile", json={"profile": "prod"}, headers=HEADERS)
    assert bad.status_code == 422
    await c.procs.start("payments-mock")
    blocked = await client.post("/api/profile", json={"profile": "kind"}, headers=HEADERS)
    assert blocked.status_code == 409
    assert error(blocked)["processes"] == ["payments-mock"]
    await c.procs.stop("payments-mock")
    ok = await client.post("/api/profile", json={"profile": "kind"}, headers=HEADERS)
    assert ok.status_code == 200 and ok.json()["profile"] == "kind"
    assert c.settings.profile == "kind"


async def test_processes_and_actions_routes(
    api: tuple[httpx.AsyncClient, Console], runner: FakeRunner
) -> None:
    client, _ = api
    started = await client.post(
        "/api/processes/worker-finance/start", json={"variant": "scripted"}, headers=HEADERS
    )
    assert started.status_code == 200 and started.json()["status"] == "running"
    live = await client.post(
        "/api/processes/worker-support/start", json={"variant": "live"}, headers=HEADERS
    )
    assert live.status_code == 409 and error(live)["code"] == "confirm_required"
    stopped = await client.post("/api/processes/worker-finance/stop", json={}, headers=HEADERS)
    assert stopped.json()["status"] == "stopped"  # the console stopped it: not "exited"
    job = await client.post("/api/actions/nexus-endpoint", json={"params": {}}, headers=HEADERS)
    assert job.status_code == 202 and job.json()["command"] == "make nexus-endpoint"
    jobs = (await client.get("/api/jobs")).json()["jobs"]
    assert jobs[0]["id"] == job.json()["id"]
    one = await client.get(f"/api/jobs/{jobs[0]['id']}")
    assert one.status_code == 200 and one.json()["log_tail"][0] == "$ make nexus-endpoint"
    gated = await client.post("/api/actions/kind-up", json={}, headers=HEADERS)
    assert gated.status_code == 409  # not on profile local
