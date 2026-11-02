"""Who ran Support, and when (DESIGN.md 4.4): the compute rail's sources, all without AWS.

- **Identities in history.** AgentCore-shaped Workers call themselves
  ``<pid>@<host>/agentcore-lifetime-<n>`` (``support_agent.agentcore_app``); a plain Worker is
  ``<pid>@<host>``, so a new pid is a restart (crash demo). Grouped like
  ``scripts/serverless_report.py``.
- **The managed AgentCore app's log** (``lifetime N: connected / polling ... identity X /
  idle for / drained``, ``worker lifetime N over; async task released``).
- **The emulator's JSONL** (``.temporal/agentcore-emulator.jsonl``): each WCI invocation.
- **The WCI system Workflow** ``temporal-sys-worker-controller-instance:<deployment>:<build>``
  in ``agents``, read by ID and walked back through ``continued_execution_run_id`` (the SDK
  version of ``scripts/wci-history.sh``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowHistory

from demo_console.clients import Gateway, NotFound
from demo_console.payloads import parse_iso, ts
from demo_console.settings import AGENTS_NS, DEPLOYMENT_NAME

E = EventType
AGENTCORE_IDENTITY = re.compile(r"^(?P<pid>\d+)@(?P<host>[^/]+)/agentcore-lifetime-(?P<n>\d+)$")
PLAIN_IDENTITY = re.compile(r"^(?P<pid>\d+)@(?P<host>.+)$")


@dataclass
class Lifetime:
    identity: str
    first: datetime
    last: datetime
    workflow_tasks: int = 0
    activities: int = 0
    #: ``agentcore-lifetime-N``: N; plain Workers: None.
    agentcore_n: int | None = None
    end: datetime | None = None
    end_reason: str | None = None
    sources: set[str] = field(default_factory=lambda: {"history"})

    @property
    def pid(self) -> str:
        return self.identity.split("@", 1)[0]


def lifetimes_from_history(history: WorkflowHistory) -> list[Lifetime]:
    """Support Worker identities in order of first appearance (WFT + Activity events)."""
    by_identity: dict[str, Lifetime] = {}

    def touch(identity: str, when: datetime | None) -> Lifetime | None:
        if not identity or when is None:
            return None
        life = by_identity.get(identity)
        if life is None:
            m = AGENTCORE_IDENTITY.match(identity)
            life = Lifetime(
                identity=identity,
                first=when,
                last=when,
                agentcore_n=int(m.group("n")) if m else None,
            )
            by_identity[identity] = life
        life.first = min(life.first, when)
        life.last = max(life.last, when)
        return life

    for e in history.events:
        when = ts(e.event_time)
        et = e.event_type
        if et == E.EVENT_TYPE_WORKFLOW_TASK_STARTED:
            touch(e.workflow_task_started_event_attributes.identity, when)
        elif et == E.EVENT_TYPE_WORKFLOW_TASK_COMPLETED:
            life = touch(e.workflow_task_completed_event_attributes.identity, when)
            if life:
                life.workflow_tasks += 1
        elif et == E.EVENT_TYPE_ACTIVITY_TASK_STARTED:
            life = touch(e.activity_task_started_event_attributes.identity, when)
            if life:
                life.activities += 1
        elif et == E.EVENT_TYPE_ACTIVITY_TASK_COMPLETED:
            touch(e.activity_task_completed_event_attributes.identity, when)
        elif et == E.EVENT_TYPE_ACTIVITY_TASK_FAILED:
            touch(e.activity_task_failed_event_attributes.identity, when)
    return sorted(by_identity.values(), key=lambda life: life.first)


# --- the AgentCore app's log ---------------------------------------------------------------

#: ``logging`` default asctime in the app's format: local time, comma milliseconds.
_LOG_LINE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(?P<ms>\d{3}) (?P<rest>.*)$")
_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("app_start", re.compile(r"AgentCore app: ")),
    ("connected", re.compile(r"lifetime (?P<n>\d+): connected to ")),
    ("polling", re.compile(r"lifetime (?P<n>\d+): polling .* identity (?P<identity>\S+)")),
    ("idle", re.compile(r"lifetime (?P<n>\d+): idle for ")),
    ("drained", re.compile(r"lifetime (?P<n>\d+): drained")),
    ("released", re.compile(r"worker lifetime (?P<n>\d+) over; async task released")),
)


@dataclass(frozen=True)
class AppEvent:
    at: datetime
    marker: str
    lifetime: int | None
    identity: str | None = None


def parse_app_log(lines: list[str]) -> list[AppEvent]:
    """Lifetime markers from the AgentCore app's stdout (local-time timestamps)."""
    out: list[AppEvent] = []
    for line in lines:
        m = _LOG_LINE.match(line)
        if not m:
            continue
        local = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S")
        at = local.replace(microsecond=int(m.group("ms")) * 1000).astimezone().astimezone(UTC)
        rest = m.group("rest")
        for marker, pattern in _MARKERS:
            hit = pattern.search(rest)
            if hit:
                groups = hit.groupdict()
                out.append(
                    AppEvent(
                        at=at,
                        marker=marker,
                        lifetime=int(groups["n"]) if groups.get("n") else None,
                        identity=groups.get("identity"),
                    )
                )
                break
    return out


def app_identities(events: list[AppEvent]) -> dict[int, str]:
    """Lifetime number -> identity, from the ``polling ... identity X`` lines (latest app)."""
    out: dict[int, str] = {}
    for ev in events:
        if ev.marker == "app_start":
            out = {}
        elif ev.marker == "polling" and ev.lifetime is not None and ev.identity:
            out[ev.lifetime] = ev.identity
    return out


def apply_app_log(lives: list[Lifetime], events: list[AppEvent]) -> None:
    """Add ends (drained / released) from the app log to the history's lifetimes."""
    ids = app_identities(events)
    by_identity = {life.identity: life for life in lives}
    for ev in events:
        if ev.lifetime is None:
            continue
        identity = ids.get(ev.lifetime)
        life = by_identity.get(identity or "")
        if life is None:
            continue
        life.sources.add("app-log")
        if ev.marker == "drained" or (ev.marker == "released" and life.end is None):
            life.end = ev.at
            life.end_reason = "drained"
        elif ev.marker == "connected":
            life.first = min(life.first, ev.at)


def app_state(events: list[AppEvent]) -> dict[str, Any]:
    """The newest lifetime the app log knows and whether it is still running."""
    latest: int | None = None
    running = False
    drained_at: datetime | None = None
    for ev in events:
        if ev.marker == "app_start":
            latest, running, drained_at = None, False, None
        elif ev.lifetime is not None:
            if ev.marker == "connected":
                latest, running, drained_at = ev.lifetime, True, None
            elif ev.marker in ("drained", "released") and ev.lifetime == latest:
                running = False
                drained_at = drained_at or ev.at
    return {"lifetime": latest, "running": running, "drained_at": drained_at}


# --- the emulator's JSONL --------------------------------------------------------------------


def read_emulator(path: Path) -> list[dict[str, Any]]:
    """``InvokeAgentRuntime`` calls the local WCI made (``scripts/agentcore_emulator.py``)."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("op") != "InvokeAgentRuntime":
            continue
        answer = entry.get("answer") or {}
        out.append(
            {
                "at": parse_iso(entry.get("time")),
                "session": entry.get("session", ""),
                "status": entry.get("status"),
                "message": answer.get("message") if isinstance(answer, dict) else None,
                "lifetime": answer.get("lifetime") if isinstance(answer, dict) else None,
            }
        )
    return out


# --- the WCI system Workflow -----------------------------------------------------------------


def wci_workflow_id(build_id: str, deployment: str = DEPLOYMENT_NAME) -> str:
    return f"temporal-sys-worker-controller-instance:{deployment}:{build_id}"


@dataclass(frozen=True)
class WciEvent:
    at: datetime
    kind: str  # wci.signal | wci.invoke | wci.failed
    detail: str


def wci_events(history: WorkflowHistory) -> list[WciEvent]:
    out: list[WciEvent] = []
    for e in history.events:
        when = ts(e.event_time)
        if when is None:
            continue
        et = e.event_type
        if et == E.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED:
            name = e.workflow_execution_signaled_event_attributes.signal_name
            out.append(WciEvent(when, "wci.signal", name))
        elif et == E.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            name = e.activity_task_scheduled_event_attributes.activity_type.name
            if name == "InvokeWorker":
                out.append(WciEvent(when, "wci.invoke", name))
        elif et == E.EVENT_TYPE_ACTIVITY_TASK_FAILED:
            msg = e.activity_task_failed_event_attributes.failure.message[:120]
            out.append(WciEvent(when, "wci.failed", msg))
    return out


class WciReader:
    """Walks the WCI's continue-as-new chain backwards; closed runs are cached."""

    def __init__(self, gateway: Gateway, max_runs: int = 60) -> None:
        self.gateway = gateway
        self.max_runs = max_runs
        self._closed: dict[str, tuple[list[WciEvent], str | None, datetime | None]] = {}

    async def events(self, build_id: str, since: datetime | None) -> list[WciEvent] | None:
        """WCI events at or after ``since``; ``None`` when there is no WCI Workflow."""
        wf = wci_workflow_id(build_id)
        try:
            latest = await self.gateway.fetch_history(AGENTS_NS, wf)
        except NotFound:
            return None
        out = wci_events(latest)
        prev = _previous_run(latest)
        first = _first_time(latest)
        runs = 1
        while prev and runs < self.max_runs and (since is None or (first and first > since)):
            cached = self._closed.get(prev)
            if cached is None:
                try:
                    hist = await self.gateway.fetch_history(AGENTS_NS, wf, prev)
                except NotFound:
                    break
                cached = (wci_events(hist), _previous_run(hist), _first_time(hist))
                self._closed[prev] = cached
            evs, prev, first = cached
            out = evs + out
            runs += 1
        if since is not None:
            out = [ev for ev in out if ev.at >= since]
        return sorted(out, key=lambda ev: ev.at)


def _previous_run(history: WorkflowHistory) -> str | None:
    for e in history.events:
        if e.event_type == E.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED:
            return e.workflow_execution_started_event_attributes.continued_execution_run_id or None
        break
    return None


def _first_time(history: WorkflowHistory) -> datetime | None:
    for e in history.events:
        return ts(e.event_time)
    return None
