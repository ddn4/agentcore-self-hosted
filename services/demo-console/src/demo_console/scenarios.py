"""Scenario catalog, preconditions and step predicates (DESIGN.md 2.2, 5.2, 5.4).

The catalog is ``data/scenarios.toml``. Every step's ``done_when`` is parsed at load time
against a closed vocabulary (an unknown predicate is a startup error) and evaluated by the
backend on a ``StepContext`` built from the run view, so steps survive reloads and restarts.
"""

from __future__ import annotations

import random
import re
import string
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

from demo_console.settings import (
    AGENTS_NS,
    DEFAULT_APPROVER,
    DEV_TQ,
    FINANCE_TQ,
    SERVICES_NS,
    SMOKE_TQ,
)

# --- predicates ------------------------------------------------------------------------------


@dataclass
class StepContext:
    """What predicates can see (built from a RunView, or from jobs for run-less scenarios)."""

    run_started: bool = False
    run_closed: bool = False
    run_status: str | None = None
    task_state: str | None = None
    lifetimes: int = 0
    drain: str | None = None
    running: bool | None = None
    nexus_attempt: int = 0
    nexus_completed: bool = False
    card_discovered: bool = False
    wci_invokes: int = 0
    processes: dict[str, str] = field(default_factory=dict)
    jobs: dict[str, str] = field(default_factory=dict)


Predicate = Callable[[StepContext], bool]
_IN = re.compile(r"^task\.state in \[(?P<items>[a-z\-, ]+)\]$")
_CMP = re.compile(r"^(?P<lhs>[a-z][\w.\-]*)(?P<op>==|>=)(?P<rhs>[\w\-]+)$")


class PredicateError(ValueError):
    pass


def parse_predicate(text: str) -> Predicate:
    """``done_when`` -> a function of ``StepContext`` (DESIGN.md 5.4)."""
    text = text.strip()
    flags: dict[str, Predicate] = {
        "run.started": lambda c: c.run_started,
        "run.closed": lambda c: c.run_closed,
        "nexus.completed": lambda c: c.nexus_completed,
        "card.discovered": lambda c: c.card_discovered,
    }
    if text in flags:
        return flags[text]
    m = _IN.match(text)
    if m:
        items = {s.strip() for s in m.group("items").split(",") if s.strip()}
        return lambda c: c.task_state in items
    m = _CMP.match(text)
    if not m:
        raise PredicateError(f"unknown predicate {text!r}")
    lhs, op, rhs = m.group("lhs"), m.group("op"), m.group("rhs")
    if op == ">=":
        if not rhs.isdigit():
            raise PredicateError(f"{text!r}: >= needs a number")
        n = int(rhs)
        counters: dict[str, Callable[[StepContext], int]] = {
            "compute.lifetimes": lambda c: c.lifetimes,
            "nexus.attempt": lambda c: c.nexus_attempt,
            "wci.invokes": lambda c: c.wci_invokes,
        }
        if lhs not in counters:
            raise PredicateError(f"unknown counter in {text!r}")
        get = counters[lhs]
        return lambda c: get(c) >= n
    if lhs == "run.status":
        return lambda c: c.run_status == rhs
    if lhs == "task.state":
        return lambda c: c.task_state == rhs
    if lhs == "compute.drain":
        if rhs != "drained":
            raise PredicateError(f"{text!r}: compute.drain only compares with drained")
        return lambda c: c.drain == "drained"
    if lhs == "compute.running":
        if rhs not in ("true", "false"):
            raise PredicateError(f"{text!r}: compute.running is true or false")
        want = rhs == "true"
        return lambda c: c.running is want
    if lhs.startswith("process."):
        name = lhs.removeprefix("process.")
        if rhs not in ("running", "stopped"):
            raise PredicateError(f"{text!r}: process state is running or stopped")
        if rhs == "running":
            return lambda c: c.processes.get(name) in ("running", "external")
        return lambda c: c.processes.get(name) not in ("running", "external", None)
    if lhs.startswith("job."):
        action = lhs.removeprefix("job.")
        return lambda c: c.jobs.get(action) == rhs
    raise PredicateError(f"unknown predicate {text!r}")


# --- the catalog -----------------------------------------------------------------------------


@dataclass
class Step:
    id: str
    n: int
    title: str
    kind: str
    done_when: str
    predicate: Predicate
    talk: str = ""
    make: str | None = None
    action: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    optional: bool = False
    #: The stepper's label (the full title is its tooltip and the notes' heading).
    short: str = ""
    #: What the presenter points at; ``point_versioned`` replaces it on a versioned run.
    point: str = ""
    point_versioned: str = ""

    def to_json(self, wf: str | None, build: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "id": self.id,
            "n": self.n,
            "title": self.title,
            "kind": self.kind,
            "done_when": self.done_when,
            "talk": self.talk,
            "short": self.short or self.title,
        }
        if self.point:
            body["point"] = self.point
        if self.point_versioned:
            body["point_versioned"] = self.point_versioned
        if self.action:
            body["action"] = self.action
        if self.params:
            body["params"] = self.params
        if self.optional:
            body["optional"] = True
        if self.make:
            body["make"] = self.make.replace("{wf}", wf or "<wf>").replace(
                "{build}", build or "<build>"
            )
        return body


@dataclass
class Scenario:
    id: str
    title: str
    short: str  # left-column list name
    sub: str  # left-column subtitle
    run_label: str  # names a run of this scenario (runs list, run view)
    summary: str
    story: dict[str, str]
    profiles: list[str]
    case: str
    order_id: str
    amount_usd: float
    id_prefix: str
    queue: str
    cost: dict[str, str]
    cost_note: str
    preconditions: list[str]
    preconditions_serverless: list[str]
    notes: str
    steps: list[Step]
    #: The scenario to suggest once a run of this one has finished (story order), or None.
    next: str | None = None
    #: The action the Start button runs (``load``, ``smoke``); ``None`` starts a Support run.
    start_action: str | None = None
    #: What the Start button says; ``{option}`` is filled from the scenario's options.
    start_label: str = "Start"
    #: ``cleanup`` shows the waiting runs instead of a Start button.
    view: str | None = None
    #: One more button next to Start: ``{action, label}``.
    extra_action: dict[str, str] | None = None
    #: The Start button's options for a run-less scenario (a Support run's are built in).
    options: list[dict[str, Any]] = field(default_factory=list)


STEP_KINDS = ("action", "watch", "human", "manual")


def load_catalog(text: str | None = None) -> dict[str, Scenario]:
    """Parse the catalog; raises ``PredicateError`` on any unknown ``done_when``."""
    if text is None:
        text = resources.files("demo_console").joinpath("data/scenarios.toml").read_text()
    data = tomllib.loads(text)
    out: dict[str, Scenario] = {}
    for raw in data.get("scenario", []):
        steps: list[Step] = []
        for n, s in enumerate(raw.get("step", []), 1):
            if s["kind"] not in STEP_KINDS:
                raise PredicateError(f"{raw['id']}/{s['id']}: unknown step kind {s['kind']!r}")
            steps.append(
                Step(
                    id=s["id"],
                    n=n,
                    title=s["title"],
                    kind=s["kind"],
                    done_when=s["done_when"],
                    predicate=parse_predicate(s["done_when"]),
                    talk=s.get("talk", ""),
                    make=s.get("make"),
                    action=s.get("action"),
                    params=dict(s.get("params", {})),
                    optional=bool(s.get("optional", False)),
                    short=s.get("short", ""),
                    point=s.get("point", ""),
                    point_versioned=s.get("point_versioned", ""),
                )
            )
        out[raw["id"]] = Scenario(
            id=raw["id"],
            title=raw["title"],
            short=raw.get("short", raw["title"]),
            sub=raw.get("sub", raw["summary"]),
            run_label=raw.get("run_label", raw.get("short", raw["title"])),
            summary=raw["summary"],
            story=dict(raw.get("story", {})),
            profiles=list(raw["profiles"]),
            case=raw.get("case", ""),
            order_id=raw.get("order_id", ""),
            amount_usd=float(raw.get("amount_usd", 0.0)),
            id_prefix=raw["id_prefix"],
            queue=raw.get("queue", "auto"),
            cost=dict(raw.get("cost", {})),
            cost_note=raw.get("cost_note", ""),
            preconditions=list(raw.get("preconditions", [])),
            preconditions_serverless=list(raw.get("preconditions_serverless", [])),
            notes=raw.get("notes", ""),
            steps=steps,
            next=raw.get("next"),
            start_action=raw.get("start_action"),
            start_label=raw.get("start_label", "Start"),
            view=raw.get("view"),
            extra_action=dict(raw["extra_action"]) if "extra_action" in raw else None,
            options=[dict(o) for o in raw.get("option", [])],
        )
    for sc in out.values():
        if sc.next is not None and sc.next not in out:
            raise PredicateError(f"{sc.id}: next names an unknown scenario {sc.next!r}")
    return out


def suffix(k: int = 4) -> str:
    """Short base-36 suffix (never 12 digits: scrub-check)."""
    alphabet = string.ascii_lowercase + string.digits
    return random.choice(string.ascii_lowercase) + "".join(
        random.choice(alphabet) for _ in range(k - 1)
    )


def default_workflow_id(scenario: Scenario) -> str:
    return f"{scenario.id_prefix}-{suffix()}"


# --- step statuses ---------------------------------------------------------------------------


def step_statuses(
    steps: list[Step],
    ctx: StepContext,
    blocked: dict[str, str] | None = None,
    sticky: set[str] | None = None,
) -> list[dict[str, Any]]:
    """``done | current | todo | skipped | blocked`` for each step (DESIGN.md 5.4).

    The last non-optional step whose predicate holds is the frontier: every non-optional step
    up to it is done (a later fact implies the earlier ones), optional ones are done or
    skipped. After the frontier steps are done in order while their predicates hold; the
    first one that is not is ``current`` (``blocked`` with ``why`` when its action can't run
    now), the rest ``todo``. ``sticky`` holds steps already seen done for this run (an optional
    "stop the worker" stays done after the worker is started again).
    """
    raw = [step.predicate(ctx) or step.id in (sticky or set()) for step in steps]
    frontier = -1
    for i, step in enumerate(steps):
        if raw[i] and not step.optional:
            frontier = i
    out: list[dict[str, Any]] = []
    current_found = False
    for i, step in enumerate(steps):
        entry: dict[str, Any] = {"id": step.id}
        if i <= frontier:
            entry["status"] = "done" if (raw[i] or not step.optional) else "skipped"
        elif not current_found and raw[i]:
            entry["status"] = "done"
        elif not current_found:
            current_found = True
            why = (blocked or {}).get(step.id)
            entry["status"] = "blocked" if why else "current"
            if why:
                entry["why"] = why
        else:
            entry["status"] = "todo"
        out.append(entry)
    return out


# --- preconditions ---------------------------------------------------------------------------


@dataclass
class StackFacts:
    """The parts of a StackSnapshot the preconditions read."""

    profile: str
    temporal_up: bool = False
    endpoint_ok: bool = False
    finance_pollers: int = 0
    support_dev_pollers: int = 0
    smoke_pollers: int = 0
    payments_up: bool = False
    agentcore_up: bool = False
    emulator_up: bool = False
    dev_server_wci: str | None = None
    build_id: str | None = None
    current_build: str | None = None
    compute: str | None = None  # none | local-wci | unknown
    managed: dict[str, bool] = field(default_factory=dict)
    shape: str = "unknown"
    #: kind only: the first missing layer (kind.gap): ``{layer, cause, fix}`` or ``None``.
    kind_gap: dict[str, Any] | None = None
    #: Reads that failed (``endpoint``, ``deployment``, ``<ns>/<tq>``): unknown, not missing.
    read_errors: dict[str, str] = field(default_factory=dict)


def _proc_fix(name: str, label: str) -> dict[str, Any]:
    return {"kind": "process", "process": name, "label": label}


def _action_fix(action: str, label: str, **params: Any) -> dict[str, Any]:
    return {"kind": "action", "action": action, "params": params, "label": label}


def _cmd_fix(command: str) -> dict[str, Any]:
    return {"kind": "command", "command": command, "label": "Copy command"}


def _outside(pid: str, label: str, who: str) -> dict[str, Any]:
    """A worker the console must own, already polling from a terminal: the console cannot stop
    it, and starting its own would only add a second poller, so there is no fix button."""
    return {
        "id": pid,
        "label": label,
        "ok": False,
        "note": f"A {who} worker started outside the console is polling. Stop it in its "
        "terminal; the console then starts its own.",
    }


#: The read each precondition depends on: when it failed the line says so, with no fix.
_READS = {
    "endpoint": "endpoint",
    "finance": f"{SERVICES_NS}/{FINANCE_TQ}",
    "finance-managed": f"{SERVICES_NS}/{FINANCE_TQ}",
    "support-plain": f"{AGENTS_NS}/{DEV_TQ}",
    "support-plain-managed": f"{AGENTS_NS}/{DEV_TQ}",
    "smoke-worker": f"{AGENTS_NS}/{SMOKE_TQ}",
    "version": "deployment",
    "version-current": "deployment",
    "version-wci": "deployment",
}


def precondition(pid: str, f: StackFacts) -> dict[str, Any] | None:
    """One precondition evaluated now; ``None`` when it does not apply on this profile.

    When the read behind it failed it is ``unknown``: not ok, no fix (the fix would create
    something that may exist), and a note with the error.
    """
    p = _precondition(pid, f)
    error = f.read_errors.get(_READS.get(pid, ""))
    if p is not None and not p["ok"] and error:
        p.pop("fix", None)
        p["unknown"] = True
        p["note"] = f"Could not read it: {error}"
    return p


def _precondition(pid: str, f: StackFacts) -> dict[str, Any] | None:
    local = f.profile == "local"
    build = f.build_id or "<build>"
    # kind: the fix for a missing layer (Docker VM, cluster, port-forward) is its action.
    below = (
        f.kind_gap
        if f.kind_gap and f.kind_gap["layer"] in ("docker", "cluster", "port-forward", "temporal")
        else None
    )
    if pid == "temporal":
        if local:
            fix = _proc_fix("dev-server", "Start dev server")
        elif below:
            fix = below["fix"]
        else:
            fix = _action_fix("pf", "Start port-forward")
        return {"id": pid, "label": "Temporal is up", "ok": f.temporal_up, "fix": fix}
    if pid == "endpoint":
        return {
            "id": pid,
            "label": "Nexus endpoint finance-agent",
            "ok": f.endpoint_ok,
            "fix": _action_fix("nexus-endpoint", "Create endpoint"),
        }
    if pid in ("finance", "finance-managed"):
        if local:
            ok = f.finance_pollers > 0
            label = "A Finance worker polls agent-services-tq"
            if pid == "finance-managed":
                ok = f.managed.get("worker-finance", False)
                label = "The console runs the Finance worker (so it can stop it)"
                if not ok and f.finance_pollers > 0:
                    return _outside(pid, label, "Finance")
            return {
                "id": pid,
                "label": label,
                "ok": ok,
                "fix": _proc_fix("worker-finance", "Start Finance worker"),
            }
        return {
            "id": pid,
            "label": "A Finance worker polls agent-services-tq",
            "ok": f.finance_pollers > 0,
            "fix": below["fix"] if below else _action_fix("kind-workers", "Deploy Finance pods"),
        }
    if pid == "payments":
        if not local:
            return None
        return {
            "id": pid,
            "label": "Payments mock on :8081",
            "ok": f.payments_up,
            "fix": _proc_fix("payments-mock", "Start payments mock"),
        }
    if pid in ("support-plain", "support-plain-managed"):
        ok = f.support_dev_pollers > 0
        label = "A Support worker polls support-dev-tq"
        if pid == "support-plain-managed":
            ok = f.managed.get("worker-support", False)
            label = "The console runs the Support worker (so it can kill it)"
            if not ok and f.support_dev_pollers > 0:
                return _outside(pid, label, "Support")
        return {
            "id": pid,
            "label": label,
            "ok": ok,
            "fix": _proc_fix("worker-support", "Start Support worker"),
        }
    if pid == "agentcore":
        return {
            "id": pid,
            "label": "AgentCore app on :8080",
            "ok": f.agentcore_up,
            "fix": _proc_fix("agentcore-local", "Start AgentCore app"),
        }
    if pid == "emulator":
        return {
            "id": pid,
            "label": "WCI emulator on :4599",
            "ok": f.emulator_up,
            "fix": _proc_fix("agentcore-emulator", "Start emulator"),
        }
    if pid == "wci":
        return {
            "id": pid,
            "label": "Dev server runs with the WCI (WCI=1)",
            "ok": f.dev_server_wci in ("1", "local"),
            "fix": _action_fix("start-recipe", "Restart with WCI", recipe="local-wci"),
        }
    if pid == "build-id":
        return {
            "id": pid,
            "label": "Support's Build ID is known",
            "ok": bool(f.build_id),
            "fix": _cmd_fix("TEMPORAL_BUILD_ID=<id> make console  # or commit uv.lock"),
        }
    if pid in ("version", "version-current", "version-wci"):
        current = bool(f.build_id) and f.current_build == f.build_id
        want = "local-wci" if pid == "version-wci" else None
        if pid == "version" and f.shape == "serverless-wci":
            want = "local-wci"
        ok = current and (want is None or f.compute == want)
        suffix_text = " with local WCI compute" if want == "local-wci" else ""
        mode = "--local-wci" if want == "local-wci" else "--poller"
        fix = _action_fix("register-version", "Register version", mode=mode)
        return {
            "id": pid,
            "label": f"support-supervisor:{build} is current{suffix_text}",
            "ok": ok,
            "fix": fix,
        }
    if pid == "serverless-compute":
        return {
            "id": pid,
            "label": "AgentCore app on :8080",
            "ok": f.agentcore_up,
            "fix": _proc_fix("agentcore-local", "Start AgentCore app"),
        }
    if pid == "smoke-worker":
        return {
            "id": pid,
            "label": "Smoke worker polls nexus-smoke-tq",
            "ok": f.smoke_pollers > 0,
            "fix": _proc_fix("worker-smoke", "Start smoke worker"),
        }
    raise KeyError(f"unknown precondition {pid!r}")


def is_serverless_shape(shape: str) -> bool:
    return shape in ("serverless-invoke", "serverless-wci")


def default_queue(scenario: Scenario, shape: str) -> str | None:
    """``dev`` | ``versioned`` | ``None`` (scenarios that don't start a Support run)."""
    if scenario.queue == "auto":
        return "versioned" if is_serverless_shape(shape) else "dev"
    if scenario.queue in ("dev", "versioned"):
        return scenario.queue
    return None


# --- setup recipes (DESIGN.md 2.2) ----------------------------------------------------------
#
# Each scenario's one-click setup is a stack recipe (actions.RECIPES; this module cannot import
# actions, a unit test checks the ids). It is derived from the scenario's queue and steps, not
# kept by hand: plain Support (support-dev-tq) is local-plain; a versioned scenario where the
# presenter invokes the AgentCore app is local-invoke, and one where the server must invoke it
# is local-wci; every kind profile brings up kind. "auto" scenarios (Case A, Case B) and the
# smoke caller run on any shape, so they keep the one the stack has.

KIND_RECIPE = "kind-up"
SHAPE_RECIPE = {
    "plain": "local-plain",
    "serverless-invoke": "local-invoke",
    "serverless-wci": "local-wci",
}
#: The preconditions each recipe fixes (the rest keep their own fix buttons).
_BASE = frozenset(("temporal", "endpoint", "finance", "finance-managed", "payments"))
_SERVERLESS = frozenset(("agentcore", "serverless-compute", "version", "version-current"))
RECIPE_FIXES: dict[str, frozenset[str]] = {
    "local-plain": _BASE | {"support-plain", "support-plain-managed"},
    "local-invoke": _BASE | _SERVERLESS,
    "local-wci": _BASE | _SERVERLESS | {"wci", "emulator", "version-wci"},
    # kind-up deploys Finance on the cluster; payments and plain Support stay on the laptop.
    KIND_RECIPE: (_BASE - {"finance-managed", "payments"}) | _SERVERLESS,
}


def shape_recipe(profile: str, shape: str) -> str | None:
    """The recipe that produces the stack's current shape (``None`` when it is unknown)."""
    if profile != "local":
        return KIND_RECIPE
    return SHAPE_RECIPE.get(shape)


def setup_recipe(scenario: Scenario, profile: str, shape: str) -> str | None:
    """The recipe that sets the stack up for ``scenario`` on ``profile`` (DESIGN.md 2.2).

    ``None`` when the scenario is not for this profile or starts nothing (Clean up).
    """
    if profile not in scenario.profiles or scenario.queue == "none":
        return None
    if profile != "local":
        return KIND_RECIPE
    if scenario.queue == "versioned":
        invokes = any(step.action == "invoke-local" for step in scenario.steps)
        return "local-invoke" if invokes else "local-wci"
    if scenario.queue == "dev":
        return "local-plain"
    return shape_recipe(profile, shape) or "local-plain"


def scenario_cost(scenario: Scenario, profile: str, model_mode: str) -> str:
    cost = scenario.cost.get(profile, "free")
    if cost == "free" and model_mode == "live":
        return "tokens"
    return cost


def next_json(
    scenario: Scenario, catalog: dict[str, Scenario] | None, facts: StackFacts
) -> dict[str, Any] | None:
    """Where the story goes after ``scenario`` on this profile (scenarios for another profile are
    skipped); ``setup_recipe`` is set only when the stack has to change shape for it."""
    catalog = catalog or {}
    nxt = catalog.get(scenario.next or "")
    for _ in range(len(catalog)):
        if nxt is None or facts.profile in nxt.profiles:
            break
        nxt = catalog.get(nxt.next or "")
    if nxt is None or facts.profile not in nxt.profiles or nxt.id == scenario.id:
        return None
    recipe = setup_recipe(nxt, facts.profile, facts.shape)
    change = recipe is not None and recipe != shape_recipe(facts.profile, facts.shape)
    return {"id": nxt.id, "short": nxt.short, "setup_recipe": recipe if change else None}


def scenario_json(
    scenario: Scenario,
    facts: StackFacts,
    model_mode: str = "scripted",
    catalog: dict[str, Scenario] | None = None,
    wf: str | None = None,
) -> dict[str, Any]:
    """``Scenario`` (DESIGN.md 5.2) with preconditions evaluated now.

    ``wf`` is the default workflow ID to offer (a fresh one when ``None``; fixtures pin it).
    """
    queue = default_queue(scenario, facts.shape)
    ids = scenario.preconditions
    if queue == "versioned" and scenario.preconditions_serverless:
        ids = scenario.preconditions_serverless
    pres = [p for p in (precondition(pid, facts) for pid in ids) if p is not None]
    recipe = setup_recipe(scenario, facts.profile, facts.shape)
    fixes = RECIPE_FIXES.get(recipe or "", frozenset())
    for p in pres:
        if p["ok"]:
            p.pop("fix", None)
        elif p["id"] in fixes and "fix" in p:
            p["by_setup"] = True
    if facts.profile not in scenario.profiles:
        fit = "wrong_profile"
    elif all(p["ok"] for p in pres):
        fit = "ready"
    else:
        fit = "needs_setup"
    # kind-up never starts the Docker VM (2.3): while it is stopped, starting it comes first.
    blocked = None
    gap = facts.kind_gap
    if recipe == KIND_RECIPE and gap and gap["layer"] == "docker" and fit == "needs_setup":
        blocked = {"why": "Start the Docker VM first.", "fix": gap["fix"]}
    wf = wf or default_workflow_id(scenario)
    # A Support run's options; a scenario whose Start runs an action has that action's params.
    options: list[dict[str, Any]] = [dict(o) for o in scenario.options]
    if queue is not None and not scenario.start_action:
        options = [
            {
                "id": "a2a_mode",
                "label": "How Support asks Finance",
                "type": "choice",
                "choices": ["tool", "handoff"],
                "default": "tool",
            },
            {
                "id": "nexus_send_timeout",
                "label": "Support's budget for Finance's answer",
                "type": "duration",
                "default": "25h",
            },
            {
                "id": "queue",
                "label": "Support's task queue",
                "type": "choice",
                "choices": ["dev", "versioned"],
                "default": queue,
            },
            {"id": "workflow_id", "label": "Workflow ID", "type": "text", "default": wf},
        ]
        if scenario.case == "B":
            options.append(
                {
                    "id": "approver",
                    "label": "Finance approver",
                    "type": "text",
                    "default": DEFAULT_APPROVER,
                }
            )
    return {
        "id": scenario.id,
        "title": scenario.title,
        "short": scenario.short,
        "sub": scenario.sub,
        "run_label": scenario.run_label,
        "summary": scenario.summary,
        "story": scenario.story,
        "profiles": scenario.profiles,
        "fit": fit,
        "cost": scenario_cost(scenario, facts.profile, model_mode),
        "cost_note": scenario.cost_note,
        "case": scenario.case or None,
        "order_id": scenario.order_id or None,
        "amount_usd": scenario.amount_usd or None,
        "options": options,
        "preconditions": pres,
        "steps": [step.to_json(wf, facts.build_id) for step in scenario.steps],
        "notes": scenario.notes,
        "setup_recipe": recipe,
        "setup_blocked": blocked,
        "next": next_json(scenario, catalog, facts),
        "start_action": scenario.start_action,
        "start_label": scenario.start_label,
        "view": scenario.view,
        "extra_action": scenario.extra_action,
    }
