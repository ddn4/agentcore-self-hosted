"""The scenario catalog and step predicates (DESIGN.md 2.2, 5.4)."""

from __future__ import annotations

import pytest

from demo_console.scenarios import (
    PredicateError,
    StackFacts,
    StepContext,
    default_queue,
    load_catalog,
    parse_predicate,
    scenario_json,
    step_statuses,
    suffix,
)

pytestmark = pytest.mark.unit


def test_every_catalog_predicate_parses() -> None:
    catalog = load_catalog()
    assert set(catalog) == {
        "case-a",
        "case-b",
        "crash",
        "serverless-b",
        "wci-b",
        "failure",
        "smoke",
        "load",
        "cleanup",
    }
    for scenario in catalog.values():
        for step in scenario.steps:
            parse_predicate(step.done_when)  # load_catalog already did; explicit here


@pytest.mark.parametrize(
    "bad",
    ["run.finished", "task.state=completed", "compute.lifetimes>=two", "compute.drain==busy", ""],
)
def test_unknown_predicates_are_errors(bad: str) -> None:
    with pytest.raises(PredicateError):
        parse_predicate(bad)
    text = f"""
[[scenario]]
id = "x"
title = "x"
summary = "x"
profiles = ["local"]
id_prefix = "x"
  [[scenario.step]]
  id = "s"
  title = "s"
  kind = "watch"
  done_when = "{bad}"
"""
    with pytest.raises(PredicateError):
        load_catalog(text)


def test_predicates() -> None:
    ctx = StepContext(
        run_started=True,
        run_status="RUNNING",
        task_state="input-required",
        lifetimes=2,
        drain="drained",
        running=False,
        nexus_attempt=3,
        card_discovered=True,
        wci_invokes=1,
        processes={"worker-support": "stopped", "worker-finance": "external"},
        jobs={"smoke": "succeeded"},
    )
    truths = {
        "run.started": True,
        "run.closed": False,
        "run.status==RUNNING": True,
        "task.state==input-required": True,
        "task.state in [completed, rejected]": False,
        "compute.lifetimes>=2": True,
        "compute.lifetimes>=3": False,
        "compute.drain==drained": True,
        "compute.running==false": True,
        "compute.running==true": False,
        "nexus.attempt>=2": True,
        "nexus.completed": False,
        "card.discovered": True,
        "process.worker-support==stopped": True,
        "process.worker-finance==running": True,
        "wci.invokes>=1": True,
        "job.smoke==succeeded": True,
    }
    for text, want in truths.items():
        assert parse_predicate(text)(ctx) is want, text


def _statuses(scenario_id: str, ctx: StepContext, sticky: set[str] | None = None) -> dict[str, str]:
    steps = load_catalog()[scenario_id].steps
    return {s["id"]: s["status"] for s in step_statuses(steps, ctx, sticky=sticky)}


def test_case_b_statuses_through_the_run() -> None:
    start = StepContext(run_started=True, running=True, run_status="RUNNING")
    assert _statuses("case-b", start) == {
        "start": "done",
        "input-required": "current",
        "stop-support": "todo",
        "approve": "todo",
        "restart-support": "todo",
        "reply": "todo",
        "report": "todo",
    }
    waiting = StepContext(run_started=True, running=True, task_state="input-required")
    assert _statuses("case-b", waiting)["stop-support"] == "current"
    stopped = StepContext(run_started=True, running=False, task_state="input-required")
    s = _statuses("case-b", stopped)
    assert s["stop-support"] == "done" and s["approve"] == "current"
    # Approved without stopping Support: the optional step is skipped.
    done = StepContext(run_started=True, running=True, task_state="completed", run_closed=True)
    s = _statuses("case-b", done)
    assert s["stop-support"] == "skipped" and s["reply"] == "done"
    # ... unless it was seen done earlier in this run (sticky).
    assert _statuses("case-b", done, sticky={"stop-support"})["stop-support"] == "done"


def test_blocked_current_step() -> None:
    steps = load_catalog()["case-b"].steps
    ctx = StepContext(run_started=True, running=False, task_state="input-required")
    out = step_statuses(steps, ctx, blocked={"approve": "The A2A task is working."})
    approve = next(s for s in out if s["id"] == "approve")
    assert approve == {"id": "approve", "status": "blocked", "why": "The A2A task is working."}


def test_serverless_b_frontier() -> None:
    before = StepContext(running=False)
    assert _statuses("serverless-b", before)["start"] == "current"
    # Started, but the lifetime the recipe invoked has drained: Invoke is the next step.
    waiting = StepContext(run_started=True, running=False, run_status="RUNNING")
    s = _statuses("serverless-b", waiting)
    assert s["start"] == "done" and s["invoke1"] == "current"
    later = StepContext(run_started=True, lifetimes=2, task_state="completed", running=True)
    s = _statuses("serverless-b", later)
    assert [
        s[k] for k in ("start", "invoke1", "input-required", "drained", "approve", "invoke2")
    ] == ["done"] * 6
    assert s["reply"] == "current"


def test_scenario_json_preconditions_and_fit() -> None:
    catalog = load_catalog()
    facts = StackFacts(profile="local", temporal_up=True, endpoint_ok=True, finance_pollers=1)
    body = scenario_json(catalog["case-a"], facts)
    assert body["fit"] == "needs_setup" and body["cost"] == "free"
    missing = [p for p in body["preconditions"] if not p["ok"]]
    assert {p["id"] for p in missing} == {"payments", "support-plain"}
    assert all("fix" in p for p in missing)
    assert missing[0]["fix"]["kind"] == "process"
    assert {o["id"] for o in body["options"]} >= {"a2a_mode", "nexus_send_timeout", "workflow_id"}
    assert body["steps"][0]["make"].startswith("make case-a WF=support-1042-")
    ready = StackFacts(
        profile="local",
        temporal_up=True,
        endpoint_ok=True,
        finance_pollers=1,
        support_dev_pollers=1,
        payments_up=True,
    )
    assert scenario_json(catalog["case-a"], ready)["fit"] == "ready"
    assert scenario_json(catalog["case-a"], ready, "live")["cost"] == "tokens"
    assert scenario_json(catalog["wci-b"], StackFacts(profile="kind"))["fit"] == "wrong_profile"
    # kind: no payments precondition (a pod), fixes are commands.
    kind = StackFacts(profile="kind", temporal_up=False)
    body = scenario_json(catalog["case-a"], kind)
    assert "payments" not in {p["id"] for p in body["preconditions"]}
    temporal = body["preconditions"][0]
    assert temporal["fix"] == {
        "kind": "command",
        "command": "make pf TEMPORAL_PROFILE=kind",
        "label": "Copy command",
    }


def test_serverless_shape_switches_queue_and_preconditions() -> None:
    catalog = load_catalog()
    assert default_queue(catalog["case-b"], "plain") == "dev"
    assert default_queue(catalog["case-b"], "serverless-wci") == "versioned"
    assert default_queue(catalog["smoke"], "plain") is None
    facts = StackFacts(profile="local", shape="serverless-wci", build_id="4ee7f87")
    ids = [p["id"] for p in scenario_json(catalog["case-b"], facts)["preconditions"]]
    assert "version" in ids and "support-plain" not in ids
    version = next(
        p for p in scenario_json(catalog["case-b"], facts)["preconditions"] if p["id"] == "version"
    )
    assert version["label"] == "support-supervisor:4ee7f87 is current with local WCI compute"
    assert version["fix"]["params"] == {"mode": "--local-wci"}


def test_suffix_is_never_twelve_digits() -> None:
    for _ in range(200):
        sfx = suffix()
        assert len(sfx) == 4 and sfx[0].isalpha()
