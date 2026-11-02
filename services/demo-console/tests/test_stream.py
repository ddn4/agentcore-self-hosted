"""The SSE stream (DESIGN.md 5.6): hello, snapshots on change only, pushes, heartbeat."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from temporalio.client import WorkflowHistory

from demo_console.app import Console, create_app
from demo_console.clients import Execution
from demo_console.fakes import FakeGateway, FakeHttp, FakeRunner, make_settings
from demo_console.stream import Intervals, digest

pytestmark = pytest.mark.unit
Load = Callable[..., WorkflowHistory]


def parse(frame: str) -> tuple[str, Any]:
    if frame.startswith(":"):
        return "comment", frame
    if frame.startswith("retry:"):
        return "retry", frame
    fields = dict(line.split(": ", 1) for line in frame.strip().splitlines())
    return fields["event"], json.loads(fields["data"])


async def collect(
    c: Console, wf: str | None, ticks: int, push: Callable[[], None] | None = None
) -> list[tuple[str, Any]]:
    count = {"n": 0}

    async def disconnected() -> bool:
        count["n"] += 1
        if push is not None and count["n"] == 3:
            push()
        return count["n"] > ticks

    return [parse(f) async for f in c.hub.events(wf, disconnected=disconnected)]


def console(tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp) -> Console:
    app = create_app(
        make_settings(tmp_path),
        clients=gateway,
        runner=runner,
        http=http,
        static_dir=tmp_path,
        intervals=Intervals(stack=0.01, runs=0.01, run=0.01, heartbeat=0.05, tick=0.01),
    )
    return app.state.console


async def test_hello_snapshots_and_rows(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp, load: Load
) -> None:
    wf = "support-1042-ca01"
    gateway.add_run(wf, load("case-a.support"), load("case-a.finance"))
    gateway.executions["agents|WorkflowType='SupportSupervisorWorkflow'"] = [
        Execution(
            wf, "run-1", "SupportSupervisorWorkflow", "COMPLETED", "support-dev-tq", None, None
        )
    ]
    c = console(tmp_path, gateway, runner, http)
    events = await collect(c, wf, ticks=12)
    names = [e for e, _ in events]
    assert names[:2] == ["retry", "hello"]
    assert events[1][1]["profile"] == "local"
    # Each snapshot is sent once: nothing changes between polls.
    for name in ("stack", "runs", "run", "rows"):
        assert names.count(name) == 1, (name, names)
    rows = next(d for e, d in events if e == "rows")
    assert rows["reset"] is True and rows["workflow_id"] == wf and rows["rows"][0]["seq"] == 1
    run = next(d for e, d in events if e == "run")
    assert run["status"] == "COMPLETED"
    assert "comment" in names  # the heartbeat
    assert gateway.support_queries() == []


async def test_pushes_and_missing_run(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp
) -> None:
    c = console(tmp_path, gateway, runner, http)
    events = await collect(c, "support-nope", ticks=6, push=lambda: c.bus.alert("warn", "hi"))
    alerts = [d for e, d in events if e == "alert"]
    assert any(a["message"] == "No run support-nope in namespace agents." for a in alerts)
    assert any(a["message"] == "hi" for a in alerts)
    assert sum(1 for a in alerts if a["message"].startswith("No run")) == 1


async def test_unreachable_alert_once(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp
) -> None:
    gateway.up = False
    c = console(tmp_path, gateway, runner, http)
    events = await collect(c, None, ticks=8)
    alerts = [d for e, d in events if e == "alert"]
    assert len(alerts) == 1 and alerts[0]["level"] == "error"
    assert "not reachable" in alerts[0]["message"]
    stack = next(d for e, d in events if e == "stack")
    assert stack["components"][0]["status"] == "down"


def test_digest_ignores_checked_at() -> None:
    a = {"checked_at": "1", "x": 1}
    b = {"checked_at": "2", "x": 1}
    assert digest(a, drop=("checked_at",)) == digest(b, drop=("checked_at",))
    assert digest(a) != digest(b)
