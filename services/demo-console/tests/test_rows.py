"""Lane rows from recorded histories (DESIGN.md 5.3 rows; scripted local runs, scrubbed)."""

from __future__ import annotations

import itertools
import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowHistory

from demo_console.lifetimes import (
    WciEvent,
    app_identities,
    app_state,
    apply_app_log,
    lifetimes_from_history,
    parse_app_log,
    wci_events,
)
from demo_console.payloads import now as utcnow
from demo_console.payloads import ts
from demo_console.rows import (
    attempt_rows,
    finalize,
    finance_rows,
    lifetime_rows,
    support_rows,
    wci_rows,
)
from demo_console.runs import apply_kills

pytestmark = pytest.mark.unit
Load = Callable[..., WorkflowHistory]
UI = "http://127.0.0.1:8233"


def all_rows(load: Load, name: str, wf: str) -> list[dict]:
    sup = load(f"{name}.support", wf)
    fin = load(f"{name}.finance", f"a2a-task-{wf}")
    rows = support_rows(sup, ui_base=UI, workflow_id=wf, finance_task_id=f"a2a-task-{wf}")
    send = next(r for r in rows if r.extra.get("operation") == "send_message")
    frows, _ = finance_rows(fin, ui_base=UI, task_id=f"a2a-task-{wf}", send_pair=send.extra["pair"])
    t0 = ts(sup.events[0].event_time)
    assert t0 is not None
    return finalize(rows + frows, t0)


def test_case_a_pairs_across_the_seam(load: Load) -> None:
    rows = all_rows(load, "case-a", "support-1042-ca01")
    kinds = [r["kind"] for r in rows]
    assert kinds[0] == "run.started" and kinds[-1] == "run.completed"
    # Discovery: one pair, both ends on the seam.
    card = [r for r in rows if r.get("operation") == "get_agent_card"]
    assert [r["kind"] for r in card] == ["nexus.scheduled", "nexus.completed"]
    assert {r["lane"] for r in card} == {"nexus"} and card[0]["pair"] == card[1]["pair"]
    # send_message out: nexus.scheduled <-> task.started; back: final task.state <-> completed.
    out = [r for r in rows if r.get("pair") == "nx-64"]
    assert [(r["lane"], r["kind"]) for r in out] == [
        ("nexus", "nexus.scheduled"),
        ("finance", "task.started"),
    ]
    back = [r for r in rows if r.get("pair") == "nx-64-back"]
    assert [(r["kind"], r.get("state")) for r in back] == [
        ("task.state", "completed"),
        ("nexus.completed", "completed"),
    ]
    agents = {r["agent"] for r in rows if r["kind"] == "model.turn"}
    assert {"Support Supervisor", "Order Lookup agent", "Return Policy agent"} <= agents
    assert "Finance Refund agent" in agents
    refund = next(r for r in rows if r["kind"] == "refund.issued")
    assert refund["refund_id"] == "R-10001"
    assert rows[0]["link"].endswith("/namespaces/agents/workflows/support-1042-ca01")


def test_seq_is_stable_and_ordered(load: Load) -> None:
    first = all_rows(load, "case-b-approve", "support-2077-cb01")
    second = all_rows(load, "case-b-approve", "support-2077-cb01")
    assert first == second
    assert [r["seq"] for r in first] == list(range(1, len(first) + 1))
    assert all(a["at"] <= b["at"] for a, b in itertools.pairwise(first))


def test_case_b_approve_and_reject_states(load: Load) -> None:
    approve = all_rows(load, "case-b-approve", "support-2077-cb01")
    states = [r.get("state") for r in approve if r["kind"] in ("task.started", "task.state")]
    assert states == ["submitted", "working", "input-required", "completed"]
    signal = next(r for r in approve if r["kind"] == "signal.approve")
    assert signal["lane"] == "human" and signal["approver"] == "dana.finance"
    assert signal["title"] == "Refund approved by dana.finance"
    asks = next(r for r in approve if r.get("state") == "input-required")
    assert (
        asks["title"] == "Finance asks a human" and asks["detail"] == "Approval timer started (24h)"
    )

    reject = all_rows(load, "case-b-reject", "support-2077-rj01")
    assert next(r for r in reject if r["kind"] == "signal.reject")["detail"] == "no photos"
    final = [r for r in reject if r["kind"] == "task.state"][-1]
    assert final["state"] == "rejected" and "rejected" in final["detail"]
    assert not any(r["kind"] == "refund.issued" for r in reject)


def test_finance_facts_from_history(load: Load) -> None:
    fin = load("case-b-approve.finance", "a2a-task-support-2077-cb01")
    _, facts = finance_rows(fin, ui_base=UI, task_id=fin.workflow_id, send_pair=None)
    assert facts.state == "completed" and facts.closed
    assert facts.signal == {
        "verdict": "approve",
        "approver": "dana.finance",
        "comment": "photos checked",
    }
    assert facts.timer_outcome == "canceled" and facts.approval_timeout_s == 86400
    assert facts.artifact and facts.artifact["approved_by"] == "dana.finance"
    # Mid-flight: cut after the approval timer started -> input-required, no query needed.
    events = list(fin.events)
    cut = next(
        i for i, e in enumerate(events) if e.event_type == EventType.EVENT_TYPE_TIMER_STARTED
    )
    _, mid = finance_rows(
        WorkflowHistory(fin.workflow_id, events[: cut + 1]),
        ui_base=UI,
        task_id=fin.workflow_id,
        send_pair=None,
    )
    assert mid.state == "input-required" and not mid.closed


def test_crash_two_pids_and_a_kill(load: Load) -> None:
    sup = load("crash.support", "support-1042-crash-k1")
    lives = lifetimes_from_history(sup)
    assert [life.identity for life in lives] == ["84397@laptop", "87104@laptop"]
    assert all(life.agentcore_n is None for life in lives)
    kill_at = "2026-11-02T16:10:54.601Z"
    apply_kills(lives, [{"name": "worker-support", "at": kill_at, "pid": 4001}])
    assert lives[0].end_reason == "killed" and lives[1].end_reason is None
    rows = lifetime_rows(lives, [])
    kinds = [(r.kind, r.title) for r in rows]
    assert ("lifetime.killed", "Worker 84397 killed (SIGKILL)") in kinds
    assert ("lifetime.start", "Worker 87104 starts") in kinds
    srows = support_rows(sup, ui_base=UI, workflow_id=sup.workflow_id, finance_task_id="x")
    retried = [r for r in srows if r.extra.get("attempt") == 2]
    assert retried and retried[0].extra["identity"] == "87104@laptop"
    assert any(r.title == "Workflow Task on a new worker" for r in srows)


def test_failure_attempt_rows_from_describe() -> None:
    now = utcnow()
    rows = attempt_rows(
        [
            {
                "operation": "send_message",
                "attempt": 3,
                "last_failure": "upstream timeout",
                "_last_attempt_at": now,
            }
        ]
    )
    assert [(r.kind, r.title, r.extra["detail"]) for r in rows] == [
        ("nexus.attempt_failed", "send_message attempt 2 failed", "upstream timeout")
    ]
    assert rows[0].extra["attempt"] == 2  # the failed attempt, as the page labels it
    assert attempt_rows([{"operation": "send_message", "attempt": 1, "_scheduled_at": now}]) == []


@pytest.fixture
def eastern() -> Iterator[None]:
    """The app log fixture was written at UTC-5 (EST); parse it there."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "EST5EDT"
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


def test_app_log_lifetimes_and_drain(load: Load, fixtures_dir: Path, eastern: None) -> None:
    events = parse_app_log((fixtures_dir / "agentcore-app.log").read_text().splitlines())
    ids = app_identities(events)
    assert ids[1].endswith("/agentcore-lifetime-1") and ids[2].endswith("/agentcore-lifetime-2")
    state = app_state(events)
    assert state["lifetime"] == 5 and state["running"] is False
    sup = load("serverless-b.support", "support-2077-sl-s1")
    lives = lifetimes_from_history(sup)
    assert [life.agentcore_n for life in lives] == [1, 2]
    apply_app_log(lives, events)
    assert lives[0].end_reason == "drained"
    assert lives[0].end is not None and lives[0].end.isoformat().startswith(
        "2026-11-02T16:15:01.911"
    )
    rows = lifetime_rows(lives, events)
    kinds = [r.kind for r in rows]
    assert kinds.count("lifetime.start") == 2 and "lifetime.drained" in kinds
    assert all(r.extra["identity"] in {life.identity for life in lives} for r in rows)


def test_wci_rows_and_window(load: Load) -> None:
    run0 = load("wci.run0", "temporal-sys-worker-controller-instance:support-supervisor:4ee7f87")
    evs = wci_events(run0)
    assert {e.kind for e in evs} <= {"wci.signal", "wci.invoke", "wci.failed"}
    assert any(e.kind == "wci.invoke" for e in evs)
    sup = load("wci-b.support", "support-2077-wci-w1")
    start = ts(sup.events[0].event_time)
    end = ts(sup.events[-1].event_time)
    assert start and end
    rows = wci_rows(evs, start, end)
    assert rows and all(
        start.timestamp() - 2 <= r.at.timestamp() <= end.timestamp() + 2 for r in rows
    )
    assert {r.extra["source"] for r in rows} == {"wci-history"}
    outside = [WciEvent(end.replace(year=2030), "wci.invoke", "InvokeWorker")]
    assert wci_rows(outside, start, end) == []
