"""RunView from history + describe (DESIGN.md 5.3), against FakeGateway."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowHistory

from demo_console.app import Console, create_app
from demo_console.fakes import FakeGateway, FakeHttp, FakeRunner, make_settings, task_value, until
from demo_console.lifetimes import wci_workflow_id

pytestmark = pytest.mark.unit
Load = Callable[..., WorkflowHistory]
E = EventType


def console(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp, **env: str
) -> Console:
    app = create_app(
        make_settings(tmp_path, **env),
        clients=gateway,
        runner=runner,
        http=http,
        static_dir=tmp_path / "no-static",
    )
    return app.state.console


async def test_case_b_completed(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp, load: Load
) -> None:
    wf = "support-2077-cb01"
    gateway.add_run(wf, load("case-b-approve.support"), load("case-b-approve.finance"))
    c = console(tmp_path, gateway, runner, http)
    view = await c.runs.view(wf)
    assert view["status"] == "COMPLETED" and view["scenario"] == "case-b"
    assert view["case"] == "B" and view["order_id"] == "2077" and view["amount_usd"] == 450.0
    assert view["ticket"].startswith("Order #2077") and view["a2a_mode"] == "tool"
    assert view["nexus_send_timeout_text"] == "25h" and view["task_queue"] == "support-dev-tq"
    task = view["task"]
    assert task["state"] == "completed" and task["source"] == "history"
    assert task["artifact"]["approved_by"] == "dana.finance"
    assert task["approval"] == {"timeout_text": "24h", "deadline": "2026-11-03T16:09:20.304Z"}
    assert view["reply"] == {"text": view["reply"]["text"], "refund_id": "R-10002"}
    assert "R-10002" in view["reply"]["text"]
    m = view["metrics"]
    assert m["held_open_s"] == pytest.approx(44.454, abs=0.01)
    assert m["approval_wait_s"] == pytest.approx(42.34, abs=0.05)
    assert m["approval_to_reply_s"] == pytest.approx(5.01, abs=0.05)
    assert m["lifetimes"] == 2 and m["q15"] is None
    assert view["links"]["support"].endswith(f"/namespaces/agents/workflows/{wf}/run-1/history")
    assert view["links"]["finance"].endswith(f"/namespaces/agent-services/workflows/a2a-task-{wf}")
    assert {s["id"]: s["status"] for s in view["steps"]}["reply"] == "done"
    assert gateway.support_queries() == []


async def test_get_task_query_while_running_and_history_fallback(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp, load: Load
) -> None:
    wf = "support-2077-cb01"
    sup = until(load("case-b-approve.support"), E.EVENT_TYPE_NEXUS_OPERATION_STARTED, 1)
    fin = until(load("case-b-approve.finance"), E.EVENT_TYPE_TIMER_STARTED)
    text = "Refund of $450.00 for order 2077 needs a Finance approver"
    gateway.add_run(wf, sup, fin, task=task_value("input-required", text))
    c = console(tmp_path, gateway, runner, http)
    view = await c.runs.view(wf, fresh=True)
    assert view["status"] == "RUNNING"
    assert view["task"]["state"] == "input-required" and view["task"]["source"] == "query"
    assert view["task"]["message"] == text
    pending = view["support"]["pending_nexus"]
    assert pending == []  # this describe has none; see test_failure_attempts
    assert view["metrics"]["held_open_s"] > 0
    steps = {s["id"]: s["status"] for s in view["steps"]}
    assert steps["input-required"] == "done" and steps["reply"] == "todo"

    # No Finance poller: get_task times out -> the task's own history answers.
    gateway.queries.clear()
    view = await c.runs.view(wf, fresh=True)
    assert view["task"]["state"] == "input-required" and view["task"]["source"] == "history"
    assert gateway.support_queries() == []


async def test_failure_attempts_from_describe(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp, load: Load
) -> None:
    wf = "support-1042-failure-f1"
    sup = until(load("failure.support"), E.EVENT_TYPE_NEXUS_OPERATION_SCHEDULED, 2)
    gateway.add_run(
        wf,
        sup,
        None,
        pending=[{"operation": "send_message", "attempt": 3, "failure": "upstream timeout"}],
    )
    c = console(tmp_path, gateway, runner, http)
    c.runs.save_record(wf, scenario="failure")
    view = await c.runs.view(wf)
    assert view["scenario"] == "failure"
    assert view["support"]["pending_nexus"][0]["attempt"] == 3
    assert view["support"]["pending_nexus"][0]["last_failure"] == "upstream timeout"
    assert view["task"]["state"] is None
    rows = (await c.runs.rows(wf))["rows"]
    assert any(r["kind"] == "nexus.attempt_failed" for r in rows)
    steps = {s["id"]: s["status"] for s in view["steps"]}
    assert steps["card"] == "done" and steps["retrying"] == "done"


async def test_serverless_wci_q15_and_lifetimes(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp, load: Load
) -> None:
    wf = "support-2077-wci-w1"
    gateway.add_run(wf, load("wci-b.support"), load("wci-b.finance"))
    wci = wci_workflow_id("4ee7f87")
    run0 = load("wci.run0", wci)
    gateway.histories[("agents", wci)] = run0
    c = console(tmp_path, gateway, runner, http)
    c.runs.save_record(wf, scenario="wci-b")
    view = await c.runs.view(wf)
    assert view["versioning"] == {
        "deployment": "support-supervisor",
        "build_id": "4ee7f87",
        "behavior": "PINNED",
    }
    assert view["metrics"]["build_ids"] == ["4ee7f87"]
    assert view["metrics"]["lifetimes"] == 2
    # Per run, with the app's own number (its identity suffix) when it differs.
    assert [life["label"] for life in view["compute"]["lifetimes"]] == [
        "lifetime 1 (app lifetime 3)",
        "lifetime 2 (app lifetime 4)",
    ]
    q15 = view["metrics"]["q15"]
    assert q15 is not None and q15["invoke_worker_s"] is not None and q15["reply_s"] < 2
    assert view["wci"]["invokes"] >= 1 and view["wci"]["rows_source"] == "wci-history"
    steps = [s["status"] for s in view["steps"]]
    assert steps == ["done"] * len(steps)
    report = await c.runs.report(wf)
    assert report["finance"]["signal"]["approver"] == "dana.finance"
    assert report["finance"]["timer"] == "canceled"
    assert report["support_during_wait"]["workflow_tasks"] == 1
    assert report["support_during_wait"]["same_worker_after"] is False
    assert report["build_ids"] == ["4ee7f87"] and report["behaviors"] == ["PINNED"]
    assert len(report["lifetimes"]) == 2


async def test_plain_worker_lifetime_ends_when_the_process_stopped(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp, load: Load
) -> None:
    """Not at its last history event: it was up (and polling) until it was stopped."""
    import json
    from datetime import timedelta

    from demo_console.lifetimes import lifetimes_from_history
    from demo_console.payloads import iso

    wf = "support-2077-cb01"
    sup = until(load("case-b-approve.support"), E.EVENT_TYPE_NEXUS_OPERATION_STARTED, 1)
    fin = until(load("case-b-approve.finance"), E.EVENT_TYPE_TIMER_STARTED)
    gateway.add_run(wf, sup, fin)
    life = lifetimes_from_history(sup)[-1]
    c = console(tmp_path, gateway, runner, http)

    # No process record, no poller: the last history event is all there is.
    view = await c.runs.view(wf, fresh=True)
    assert view["compute"]["running"] is False
    assert view["compute"]["lifetimes"][-1]["end"] == iso(life.last)

    # The server's last poll from that identity (kept after the poller list drops it).
    polled = life.last + timedelta(seconds=70)
    c.stack.poller_seen[life.identity] = polled
    view = await c.runs.view(wf, fresh=True)
    assert view["compute"]["lifetimes"][-1]["end"] == iso(polled)

    # A managed process that ended (kill -9 in a terminal included) wins; an exit of a
    # process started after this lifetime began is someone else's.
    killed = life.last + timedelta(seconds=110)
    with c.procs.exits_path.open("a") as f:
        later = {"at": iso(killed + timedelta(minutes=5)), "started_at": iso(killed)}
        f.write(json.dumps({"name": "worker-support", **later}) + "\n")
        mine = {"at": iso(killed), "started_at": iso(life.first - timedelta(seconds=30))}
        f.write(json.dumps({"name": "worker-support", **mine}) + "\n")
    view = await c.runs.view(wf, fresh=True)
    last = view["compute"]["lifetimes"][-1]
    assert last["end"] == iso(killed) and last["end_reason"] == "stopped"


async def test_scenario_inference(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp, load: Load
) -> None:
    a, sl = "support-1042-ca01", "support-2077-sl-s1"
    gateway.add_run(a, load("case-a.support"), load("case-a.finance"))
    gateway.add_run(sl, load("serverless-b.support"), load("serverless-b.finance"))
    c = console(tmp_path, gateway, runner, http)
    assert (await c.runs.view(a))["scenario"] == "case-a"
    assert (await c.runs.view(sl))["scenario"] == "serverless-b"
    c.stack.last = {"wci": {"mode": "emulator"}}
    assert (await c.runs.view(sl, fresh=True))["scenario"] == "wci-b"
    c.runs.save_record(sl, scenario="case-b")  # what the console started wins
    assert (await c.runs.view(sl, fresh=True))["scenario"] == "case-b"
    (tmp_path / "eks").mkdir()
    eks = console(tmp_path / "eks", gateway, runner, http)
    eks.settings.profile = "eks-pf"
    assert (await eks.runs.view(sl))["scenario"] == "cloud-b"
    # a2a-task-<wf> addresses the same run.
    assert (await c.runs.view(f"a2a-task-{a}"))["workflow_id"] == a


async def test_eks_drain_is_estimated(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp, load: Load
) -> None:
    wf = "support-2077-sl-s1"
    sup = until(load("serverless-b.support"), E.EVENT_TYPE_NEXUS_OPERATION_STARTED, 1)
    fin = until(load("serverless-b.finance"), E.EVENT_TYPE_TIMER_STARTED)
    gateway.add_run(wf, sup, fin)
    (tmp_path / "eks").mkdir()
    c = console(tmp_path / "eks", gateway, runner, http)
    c.settings.profile = "eks-pf"
    view = await c.runs.view(wf)
    drain = view["compute"]["drain"]
    assert drain["state"] == "drained" and drain["estimated"] is True
    assert drain["source"] == "estimate" and view["compute"]["running"] is False
    assert view["scenario"] == "cloud-b"
    # A CloudWatch confirmation (opt-in action) replaces the estimate.
    c.runs.save_record(wf, cloudwatch={"drained_at": "2026-11-02T16:15:02.000Z", "sessions": []})
    view = await c.runs.view(wf, fresh=True)
    assert view["compute"]["drain"] == {
        "state": "drained",
        "at": "2026-11-02T16:15:02.000Z",
        "estimated": False,
        "source": "cloudwatch",
    }


async def test_rows_after_and_version(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp, load: Load
) -> None:
    wf = "support-1042-ca01"
    full_sup = load("case-a.support")
    gateway.add_run(wf, until(full_sup, E.EVENT_TYPE_NEXUS_OPERATION_SCHEDULED, 2))
    c = console(tmp_path, gateway, runner, http)
    first = await c.runs.rows(wf)
    assert first["t0"] == "2026-11-02T16:07:53.055Z"
    assert first["rows"][0]["t"] == 0.0
    gateway.add_run(wf, full_sup, load("case-a.finance"))
    second = await c.runs.view(wf, fresh=True)
    assert second["rows_version"] == first["rows_version"] + 1
    tail = await c.runs.rows(wf, after=len(first["rows"]))
    assert tail["rows"] and tail["rows"][0]["seq"] == len(first["rows"]) + 1
    reset, new = c.runs.rows_delta(
        wf,
        [(r["at"], r["lane"], r["kind"], r["title"]) for r in first["rows"]],
        (await c.runs.rows(wf))["rows"],
    )
    assert reset is False and len(new) == len(tail["rows"])
