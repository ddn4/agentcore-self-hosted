"""The scenario catalog and step predicates (DESIGN.md 2.2, 5.4)."""

from __future__ import annotations

import tomllib
from importlib.resources import files
from typing import Any

import pytest

from demo_console.actions import RECIPES
from demo_console.scenarios import (
    RECIPE_FIXES,
    SHAPE_RECIPE,
    PredicateError,
    StackFacts,
    StepContext,
    default_queue,
    load_catalog,
    parse_predicate,
    scenario_json,
    setup_recipe,
    step_statuses,
    suffix,
)
from demo_console.settings import PROFILES

pytestmark = pytest.mark.unit


def test_every_catalog_predicate_parses() -> None:
    catalog = load_catalog()
    assert set(catalog) == {
        "case-a",
        "case-b",
        "crash",
        "serverless-b",
        "wci-b",
        "wci-crash",
        "failure",
        "smoke",
        "load",
        "cleanup",
    }
    for scenario in catalog.values():
        for step in scenario.steps:
            parse_predicate(step.done_when)  # load_catalog already did; explicit here


def test_catalog_profiles_are_known_and_costs_match_them() -> None:
    for scenario in load_catalog().values():
        assert scenario.profiles, scenario.id
        assert set(scenario.profiles) <= set(PROFILES), scenario.id
        assert set(scenario.cost) <= set(scenario.profiles), scenario.id


def test_catalog_names_every_scenario_explicitly() -> None:
    text = (files("demo_console") / "data" / "scenarios.toml").read_text()
    for raw in tomllib.loads(text)["scenario"]:
        for key in ("title", "short", "sub", "run_label"):
            assert raw.get(key), f"{raw['id']} has no {key}"


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
    # kind: no payments precondition (a pod); fixes are the console's own actions.
    kind = StackFacts(profile="kind", temporal_up=False)
    body = scenario_json(catalog["case-a"], kind)
    assert "payments" not in {p["id"] for p in body["preconditions"]}
    pre = {p["id"]: p for p in body["preconditions"]}
    assert pre["temporal"]["fix"] == {
        "kind": "action",
        "action": "pf",
        "params": {},
        "label": "Start port-forward",
    }
    assert pre["finance"]["fix"]["action"] == "kind-workers"
    # With the Docker VM stopped, both point at the first missing layer instead.
    stopped = {
        "layer": "docker",
        "cause": "Docker VM agentcore is stopped",
        "fix": {"kind": "action", "action": "colima-up", "params": {}, "label": "Start Docker VM"},
    }
    cold = StackFacts(profile="kind", kind_gap=stopped)
    pre = {p["id"]: p for p in scenario_json(catalog["case-a"], cold)["preconditions"]}
    assert pre["temporal"]["fix"]["action"] == "colima-up"
    assert pre["finance"]["fix"]["action"] == "colima-up"


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


# --- one-click setup and the next scenario (DESIGN.md 2.2) ---------------------------------


def test_setup_recipe_follows_queue_and_shape() -> None:
    c = load_catalog()

    def local(shape: str) -> dict[str, str | None]:
        return {sid: setup_recipe(s, "local", shape) for sid, s in c.items()}

    cold = local("unknown")
    # Plain Support (support-dev-tq) is local-plain whatever the stack is now.
    assert cold["crash"] == cold["failure"] == "local-plain"
    # The presenter invokes the app -> invoke-local; the server must invoke it -> the WCI.
    assert cold["serverless-b"] == "local-invoke"
    assert cold["wci-b"] == cold["wci-crash"] == cold["load"] == "local-wci"
    # Case A, Case B and the smoke caller run on any shape: keep the stack's, or plain.
    assert cold["case-a"] == cold["case-b"] == cold["smoke"] == "local-plain"
    assert local("serverless-wci")["case-b"] == "local-wci"
    assert local("serverless-invoke")["case-a"] == "local-invoke"
    assert local("serverless-invoke")["crash"] == "local-plain"
    # Clean up starts nothing.
    assert cold["cleanup"] is None
    # Every kind profile brings up kind; local-only scenarios have no recipe there.
    for profile in ("kind", "kind-plaintext"):
        assert setup_recipe(c["case-b"], profile, "unknown") == "kind-up"
        assert setup_recipe(c["wci-b"], profile, "plain") is None
        assert setup_recipe(c["wci-crash"], profile, "plain") is None


def test_setup_recipes_exist_and_cover_what_they_claim() -> None:
    named = {*SHAPE_RECIPE.values(), *RECIPE_FIXES}
    assert named <= set(RECIPES)
    for sid, s in load_catalog().items():
        for profile in PROFILES:
            for shape in ("unknown", *SHAPE_RECIPE):
                recipe = setup_recipe(s, profile, shape)
                assert recipe is None or recipe in RECIPE_FIXES, (sid, profile, shape)


def test_scenario_json_marks_what_the_setup_recipe_fixes() -> None:
    c = load_catalog()
    cold = StackFacts(profile="local")
    body = scenario_json(c["smoke"], cold, catalog=c)
    assert body["setup_recipe"] == "local-plain"
    by_setup = {p["id"] for p in body["preconditions"] if p.get("by_setup")}
    # The smoke worker is not part of any recipe: it keeps its own fix button only.
    assert by_setup == {"temporal", "endpoint", "finance", "payments"}
    body = scenario_json(c["wci-b"], cold, catalog=c)
    assert body["setup_recipe"] == "local-wci"
    missing = {p["id"] for p in body["preconditions"] if not p["ok"]}
    assert {p["id"] for p in body["preconditions"] if p.get("by_setup")} == missing - {"build-id"}
    ready = StackFacts(profile="local", temporal_up=True)
    body = scenario_json(c["cleanup"], ready, catalog=c)
    assert body["setup_recipe"] is None and not any(
        p.get("by_setup") for p in body["preconditions"]
    )


def test_worker_started_outside_the_console_is_not_left_to_setup() -> None:
    # local-plain's _ensure skips a worker that is already polling, so it cannot fix these.
    c = load_catalog()
    for sid, pid, outside in (
        ("crash", "support-plain-managed", StackFacts(profile="local", support_dev_pollers=1)),
        ("failure", "finance-managed", StackFacts(profile="local", finance_pollers=1)),
    ):
        f = outside
        p = next(x for x in scenario_json(c[sid], f, catalog=c)["preconditions"] if x["id"] == pid)
        assert not p["ok"] and "fix" not in p and not p.get("by_setup")
        assert "outside the console" in p["note"]
        # Nothing polling: the setup recipe starts it under the console.
        f = StackFacts(profile="local", temporal_up=True, managed={})
        p = next(x for x in scenario_json(c[sid], f, catalog=c)["preconditions"] if x["id"] == pid)
        assert p["by_setup"] and "note" not in p


def test_agentcore_app_started_outside_the_console_is_not_left_to_setup() -> None:
    # The AgentCore crash kills the app: only one the console started can be killed, and a
    # second app cannot bind :8080, so one answering from a terminal has no fix button.
    c = load_catalog()

    def line(f: StackFacts) -> dict[str, Any]:
        body = scenario_json(c["wci-crash"], f, catalog=c)
        assert body["setup_recipe"] == "local-wci"
        return next(x for x in body["preconditions"] if x["id"] == "agentcore-managed")

    p = line(StackFacts(profile="local", agentcore_up=True))
    assert not p["ok"] and "fix" not in p and not p.get("by_setup")
    assert "outside the console" in p["note"]
    p = line(StackFacts(profile="local", temporal_up=True))
    assert p["by_setup"] and p["fix"]["process"] == "agentcore-local" and "note" not in p
    p = line(StackFacts(profile="local", agentcore_up=True, managed={"agentcore-local": True}))
    assert p["ok"] and "fix" not in p
    # Local only: the WCI emulator runs on the laptop's dev server.
    assert scenario_json(c["wci-crash"], StackFacts(profile="kind"))["fit"] == "wrong_profile"


def test_kind_setup_waits_for_the_docker_vm() -> None:
    c = load_catalog()
    fix = {"kind": "action", "action": "colima-up", "params": {}, "label": "Start Docker VM"}
    gap = {"layer": "docker", "cause": "Docker VM agentcore is stopped", "fix": fix}
    body = scenario_json(c["case-b"], StackFacts(profile="kind", kind_gap=gap), catalog=c)
    assert body["setup_recipe"] == "kind-up"
    assert body["setup_blocked"] == {"why": "Start the Docker VM first.", "fix": fix}
    body = scenario_json(c["case-b"], StackFacts(profile="kind"), catalog=c)
    assert body["setup_blocked"] is None


def test_next_links_follow_story_order() -> None:
    c = load_catalog()
    chain, sid = [], "case-a"
    while sid:
        chain.append(sid)
        sid = c[sid].next
    # Every scenario once, in the catalog's (story) order, ending at Clean up.
    assert chain == list(c) and chain[-1] == "cleanup"


def test_next_json_skips_other_profiles_and_names_a_shape_change() -> None:
    c = load_catalog()
    plain = StackFacts(profile="local", shape="plain")
    assert scenario_json(c["case-a"], plain, catalog=c)["next"] == {
        "id": "case-b",
        "short": "Case B",
        "setup_recipe": None,
    }
    # Plain -> you invoke changes the stack's shape: the link sets it up first.
    nxt = scenario_json(c["failure"], plain, catalog=c)["next"]
    assert nxt["id"] == "serverless-b" and nxt["setup_recipe"] == "local-invoke"
    wci = StackFacts(profile="local", shape="serverless-wci")
    assert scenario_json(c["wci-b"], wci, catalog=c)["next"]["setup_recipe"] is None
    # On kind, the local-only scenarios are skipped and kind-up already is the shape.
    kind = StackFacts(profile="kind", shape="serverless-invoke")
    assert scenario_json(c["crash"], kind, catalog=c)["next"]["id"] == "serverless-b"
    assert scenario_json(c["serverless-b"], kind, catalog=c)["next"] == {
        "id": "load",
        "short": "Load",
        "setup_recipe": None,
    }
    assert scenario_json(c["cleanup"], plain, catalog=c)["next"] is None
    # Without the catalog (older callers) there is no link.
    assert scenario_json(c["case-a"], plain)["next"] is None


def test_unknown_next_is_a_startup_error() -> None:
    text = (files("demo_console") / "data" / "scenarios.toml").read_text()
    with pytest.raises(PredicateError, match="unknown scenario"):
        load_catalog(text.replace('next = "case-b"', 'next = "case-z"'))


@pytest.mark.parametrize("sid", ["crash", "wci-crash"])
def test_crash_steps_need_a_real_kill(sid: str) -> None:
    # The worker runs before the kill and again after the restart, and a finished run passes
    # every required step: only a console SIGKILL of this run's worker marks Kill done.
    steps = load_catalog()[sid].steps

    def status(seen: set[str], **ctx: Any) -> dict[str, str]:
        out = step_statuses(steps, StepContext(run_started=True, **ctx), sticky=seen)
        seen.update(s["id"] for s in out if s["status"] == "done")
        return {s["id"]: s["status"] for s in out}

    seen: set[str] = set()
    assert status(seen, lifetimes=1, processes={"agentcore-local": "running"})["kill"] == "current"
    after = status(seen, lifetimes=1, killed=True)
    assert after["kill"] == "done" and after["restart"] == "current"
    # Restarted, not picked up yet (the WCI waits out the turn's heartbeat timeout).
    assert status(seen, lifetimes=1, killed=True)["restart"] == "current"
    after = status(seen, lifetimes=2, killed=True)
    assert after["restart"] == "done" and after["reply"] == "current"
    # Nobody clicked Kill: the run finished, and the crash steps say so.
    done = status(set(), lifetimes=1, run_closed=True, run_status="COMPLETED")
    assert done["kill"] == done["restart"] == "skipped" and done["reply"] == "done"


def test_a_stale_agentcore_app_is_left_to_setup() -> None:
    # The console's app runs an older Build ID than the one setup registers: not ok, and
    # Set up restarts it (the recipe of the stack's WCI mode).
    c = load_catalog()
    managed = {"agentcore-local": True}
    base = dict(profile="local", agentcore_up=True, managed=managed, build_id="6d9111a")
    for sid, pid, wci, recipe in (
        ("wci-crash", "agentcore-managed", "1", "local-wci"),
        ("wci-b", "agentcore", "1", "local-wci"),
        ("serverless-b", "agentcore", None, "local-invoke"),
    ):
        f = StackFacts(**base, dev_server_wci=wci, agentcore_stale="79663e2")  # type: ignore[arg-type]
        p = next(x for x in scenario_json(c[sid], f, catalog=c)["preconditions"] if x["id"] == pid)
        assert not p["ok"] and p["by_setup"], (sid, p)
        assert p["fix"]["params"] == {"recipe": recipe} and "79663e2" in p["note"]
        f = StackFacts(**base, dev_server_wci=wci)  # type: ignore[arg-type]
        p = next(x for x in scenario_json(c[sid], f, catalog=c)["preconditions"] if x["id"] == pid)
        assert p["ok"] and "note" not in p
