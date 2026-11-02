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

from demo_console.settings import DEFAULT_APPROVER

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

    def to_json(self, wf: str | None, build: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "id": self.id,
            "n": self.n,
            "title": self.title,
            "kind": self.kind,
            "done_when": self.done_when,
            "talk": self.talk,
        }
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
                )
            )
        out[raw["id"]] = Scenario(
            id=raw["id"],
            title=raw["title"],
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
        )
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
    compute: str | None = None  # none | local-wci | agentcore-aws | unknown
    allow_aws: bool = False
    managed: dict[str, bool] = field(default_factory=dict)
    shape: str = "unknown"


def _proc_fix(name: str, label: str) -> dict[str, Any]:
    return {"kind": "process", "process": name, "label": label}


def _action_fix(action: str, label: str, **params: Any) -> dict[str, Any]:
    return {"kind": "action", "action": action, "params": params, "label": label}


def _cmd_fix(command: str) -> dict[str, Any]:
    return {"kind": "command", "command": command, "label": "Copy command"}


def precondition(pid: str, f: StackFacts) -> dict[str, Any] | None:
    """One precondition evaluated now; ``None`` when it does not apply on this profile."""
    local = f.profile == "local"
    eks = f.profile == "eks-pf"
    build = f.build_id or "<build>"
    if pid == "temporal":
        fix = (
            _proc_fix("dev-server", "Start dev server")
            if local
            else _cmd_fix(
                "make resume && make pf TEMPORAL_PROFILE=eks-pf"
                if eks
                else f"make pf TEMPORAL_PROFILE={f.profile}"
            )
        )
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
            return {
                "id": pid,
                "label": label,
                "ok": ok,
                "fix": _proc_fix("worker-finance", "Start Finance worker"),
            }
        command = "make finance-scale N=2" if eks else "make kind-workers FINANCE_MODEL=scripted"
        return {
            "id": pid,
            "label": "A Finance worker polls agent-services-tq",
            "ok": f.finance_pollers > 0,
            "fix": _cmd_fix(command),
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
    if pid in ("version", "version-current", "version-wci", "version-aws"):
        current = bool(f.build_id) and f.current_build == f.build_id
        want = {"version-wci": "local-wci", "version-aws": "agentcore-aws"}.get(pid)
        if pid == "version" and f.shape == "serverless-wci":
            want = "local-wci"
        ok = current and (want is None or f.compute == want)
        suffix_text = {
            "local-wci": " with local WCI compute",
            "agentcore-aws": " with AgentCore compute",
        }.get(want or "", "")
        if eks:
            fix = _cmd_fix("make register-version-aws CLOUD=1 TEMPORAL_PROFILE=eks-pf")
        else:
            mode = "--local-wci" if want == "local-wci" else "--poller"
            fix = _action_fix("register-version", "Register version", mode=mode)
        return {
            "id": pid,
            "label": f"support-supervisor:{build} is current{suffix_text}",
            "ok": ok,
            "fix": fix,
        }
    if pid == "serverless-compute":
        if eks:
            return {
                "id": pid,
                "label": "AgentCore compute on the current version",
                "ok": f.compute == "agentcore-aws",
                "fix": _cmd_fix("make register-version-aws CLOUD=1 TEMPORAL_PROFILE=eks-pf"),
            }
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
    if pid == "aws":
        return {
            "id": pid,
            "label": "AWS actions allowed (CONSOLE_ALLOW_AWS=1)",
            "ok": f.allow_aws,
            "fix": _cmd_fix("TEMPORAL_PROFILE=eks-pf CONSOLE_ALLOW_AWS=1 make console"),
        }
    raise KeyError(f"unknown precondition {pid!r}")


def is_serverless_shape(shape: str) -> bool:
    return shape in ("serverless-invoke", "serverless-wci", "agentcore-aws")


def default_queue(scenario: Scenario, shape: str) -> str | None:
    """``dev`` | ``versioned`` | ``None`` (scenarios that don't start a Support run)."""
    if scenario.queue == "auto":
        return "versioned" if is_serverless_shape(shape) else "dev"
    if scenario.queue in ("dev", "versioned"):
        return scenario.queue
    return None


def scenario_cost(scenario: Scenario, profile: str, model_mode: str) -> str:
    cost = scenario.cost.get(profile, "free")
    if cost == "free" and model_mode == "live":
        return "tokens"
    return cost


def scenario_json(
    scenario: Scenario, facts: StackFacts, model_mode: str = "scripted"
) -> dict[str, Any]:
    """``Scenario`` (DESIGN.md 5.2) with preconditions evaluated now."""
    queue = default_queue(scenario, facts.shape)
    ids = scenario.preconditions
    if queue == "versioned" and scenario.preconditions_serverless:
        ids = scenario.preconditions_serverless
    pres = [p for p in (precondition(pid, facts) for pid in ids) if p is not None]
    for p in pres:
        if p["ok"]:
            p.pop("fix", None)
    if facts.profile not in scenario.profiles:
        fit = "wrong_profile"
    elif all(p["ok"] for p in pres):
        fit = "ready"
    else:
        fit = "needs_setup"
    wf = default_workflow_id(scenario)
    options: list[dict[str, Any]] = []
    if queue is not None:
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
    elif scenario.id == "load":
        options = [
            {
                "id": "n",
                "label": "Conversations",
                "type": "number",
                "default": 10,
                "min": 1,
                "max": 20,
            },
            {
                "id": "kind",
                "label": "What each one runs",
                "type": "choice",
                "choices": ["ping", "case-a"],
                "default": "ping",
            },
        ]
    return {
        "id": scenario.id,
        "title": scenario.title,
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
    }
