"""Runs: the list, the run view, its rows and the report (DESIGN.md 5.3).

Everything is derived from Temporal, so the console can be restarted at any time. Support
(namespace ``agents``) is read with ``describe`` and ``fetch_history`` only (ledger 72);
Finance's A2A task with ``get_task`` (short timeout), falling back to its history.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import itertools
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import temporalio.api.enums.v1 as enums
from temporalio.api.enums.v1 import EventType, VersioningBehavior
from temporalio.api.workflowservice.v1 import DescribeWorkflowExecutionResponse
from temporalio.client import WorkflowHistory

from demo_console.clients import Gateway, NotFound, TemporalUnreachable
from demo_console.jobs import JobStore
from demo_console.lifetimes import (
    AppEvent,
    Lifetime,
    WciEvent,
    WciReader,
    apply_app_log,
    lifetimes_from_history,
    parse_app_log,
)
from demo_console.payloads import decode, first, iso, now, parse_iso, seconds, ts
from demo_console.procs import SPECS, ProcessManager, tail_file
from demo_console.rows import (
    FinanceFacts,
    Row,
    a2a_state_from_result,
    attempt_rows,
    finalize,
    finance_rows,
    lifetime_rows,
    support_rows,
    ui_link,
    wci_rows,
)
from demo_console.scenarios import Scenario, StepContext, step_statuses
from demo_console.settings import AGENTS_NS, SERVICES_NS, VERSIONED_TQ, Settings
from demo_console.stack import StackProbe

E = EventType
SUPPORT_TYPE = "SupportSupervisorWorkflow"
FINANCE_TYPE = "FinanceAgentTaskWorkflow"
ORDER_AMOUNTS = {"1042": 40.0, "2077": 450.0}
_ORDER = re.compile(r"#\s*(\d+)")
_LOAD = re.compile(r"^(load-\d+)-\d+$")
VIEW_TTL_S = 0.8
QUERY_TIMEOUT_S = 3.0


def finance_task_id(workflow_id: str) -> str:
    return workflow_id if workflow_id.startswith("a2a-task-") else f"a2a-task-{workflow_id}"


def support_id(workflow_id: str) -> str:
    return workflow_id.removeprefix("a2a-task-")


def _status_name(value: int) -> str:
    return enums.WorkflowExecutionStatus.Name(value).removeprefix(  # type: ignore[arg-type]
        "WORKFLOW_EXECUTION_STATUS_"
    )


def _behavior(value: int) -> str | None:
    if not value:
        return None
    return VersioningBehavior.Name(value).removeprefix("VERSIONING_BEHAVIOR_")  # type: ignore[arg-type]


def order_of(ticket: str) -> str | None:
    m = _ORDER.search(ticket or "")
    return m.group(1) if m else None


@dataclass
class StaticInfo:
    """What never changes in a run: from the start event (cached per run_id)."""

    ticket: str
    a2a_mode: str
    budget: str
    task_queue: str
    started_at: datetime | None
    order_id: str | None


def static_info(history: WorkflowHistory) -> StaticInfo:
    for e in history.events:
        if e.event_type == E.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED:
            attrs = e.workflow_execution_started_event_attributes
            args = [decode(p) for p in attrs.input.payloads]
            ticket = str(args[0]) if args else ""
            return StaticInfo(
                ticket=ticket,
                a2a_mode=str(args[1]) if len(args) > 1 and args[1] else "tool",
                budget=str(args[2]) if len(args) > 2 and args[2] else "",
                task_queue=attrs.task_queue.name,
                started_at=ts(e.event_time),
                order_id=order_of(ticket),
            )
    return StaticInfo("", "tool", "", "", None, None)


@dataclass
class _Nexus:
    """Support's view of its Nexus operations."""

    card_discovered: bool = False
    send_scheduled: datetime | None = None
    send_started: datetime | None = None
    send_ended: datetime | None = None
    send_outcome: str | None = None
    send_event_id: int | None = None
    send_budget_s: float | None = None
    send_amount: float | None = None
    finance_ids: list[str] = field(default_factory=list)


def nexus_facts(history: WorkflowHistory) -> _Nexus:
    out = _Nexus()
    ops: dict[int, str] = {}
    for e in history.events:
        at = ts(e.event_time)
        et = e.event_type
        if et == E.EVENT_TYPE_NEXUS_OPERATION_SCHEDULED:
            attrs = e.nexus_operation_scheduled_event_attributes
            ops[e.event_id] = attrs.operation
            if attrs.operation == "send_message":
                out.send_scheduled, out.send_event_id = at, e.event_id
                out.send_started = out.send_ended = out.send_outcome = None
                out.send_budget_s = attrs.schedule_to_close_timeout.ToTimedelta().total_seconds()
                msg = decode(attrs.input)
                if isinstance(msg, dict):
                    for part in msg.get("parts") or []:
                        data = part.get("data") if isinstance(part, dict) else None
                        if isinstance(data, dict) and "amount_usd" in data:
                            with contextlib.suppress(TypeError, ValueError):
                                out.send_amount = float(data["amount_usd"])
        elif et == E.EVENT_TYPE_NEXUS_OPERATION_STARTED:
            sid = e.nexus_operation_started_event_attributes.scheduled_event_id
            if ops.get(sid) == "send_message":
                out.send_started = at
            for link in e.links:
                if link.HasField("workflow_event") and link.workflow_event.namespace == SERVICES_NS:
                    out.finance_ids.append(link.workflow_event.workflow_id)
        elif et in (
            E.EVENT_TYPE_NEXUS_OPERATION_COMPLETED,
            E.EVENT_TYPE_NEXUS_OPERATION_FAILED,
            E.EVENT_TYPE_NEXUS_OPERATION_TIMED_OUT,
            E.EVENT_TYPE_NEXUS_OPERATION_CANCELED,
        ):
            name = {
                E.EVENT_TYPE_NEXUS_OPERATION_COMPLETED: "completed",
                E.EVENT_TYPE_NEXUS_OPERATION_FAILED: "failed",
                E.EVENT_TYPE_NEXUS_OPERATION_TIMED_OUT: "timed_out",
                E.EVENT_TYPE_NEXUS_OPERATION_CANCELED: "canceled",
            }[et]
            sid = getattr(e, f"nexus_operation_{name}_event_attributes").scheduled_event_id
            outcome = "completed" if et == E.EVENT_TYPE_NEXUS_OPERATION_COMPLETED else "failed"
            if ops.get(sid) == "get_agent_card" and outcome == "completed":
                out.card_discovered = True
            elif ops.get(sid) == "send_message":
                out.send_ended, out.send_outcome = at, outcome
    return out


def pending_nexus(desc: DescribeWorkflowExecutionResponse) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for op in desc.pending_nexus_operations:
        state = enums.PendingNexusOperationState.Name(op.state).removeprefix(  # type: ignore[arg-type]
            "PENDING_NEXUS_OPERATION_STATE_"
        )
        failure = op.last_attempt_failure.message if op.HasField("last_attempt_failure") else None
        scheduled = ts(op.scheduled_time)
        last = ts(op.last_attempt_complete_time)
        out.append(
            {
                "operation": op.operation,
                "state": state,
                "attempt": op.attempt,
                "last_failure": failure or None,
                "schedule_to_close_s": op.schedule_to_close_timeout.ToTimedelta().total_seconds()
                or None,
                "scheduled_at": iso(scheduled),
                "_scheduled_at": scheduled,
                "_last_attempt_at": last,
            }
        )
    return out


def public(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in item.items() if not k.startswith("_")} for item in items]


def build_ids(history: WorkflowHistory) -> tuple[list[str], list[str]]:
    builds: list[str] = []
    behaviors: list[str] = []
    for e in history.events:
        if e.event_type == E.EVENT_TYPE_WORKFLOW_TASK_COMPLETED:
            a = e.workflow_task_completed_event_attributes
            b = a.deployment_version.build_id
            if b and b not in builds:
                builds.append(b)
            beh = _behavior(a.versioning_behavior)
            if beh and beh not in behaviors:
                behaviors.append(beh)
    return builds, behaviors


def first_after(history: WorkflowHistory, kind: int, after: datetime) -> datetime | None:
    for e in history.events:
        at = ts(e.event_time)
        if e.event_type == kind and at is not None and at >= after:
            return at
    return None


def close_time(history: WorkflowHistory) -> datetime | None:
    closing = {
        E.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED,
        E.EVENT_TYPE_WORKFLOW_EXECUTION_FAILED,
        E.EVENT_TYPE_WORKFLOW_EXECUTION_TERMINATED,
        E.EVENT_TYPE_WORKFLOW_EXECUTION_TIMED_OUT,
        E.EVENT_TYPE_WORKFLOW_EXECUTION_CANCELED,
        E.EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW,
    }
    for e in reversed(list(history.events)):
        if e.event_type in closing:
            return ts(e.event_time)
    return None


def reply_text(history: WorkflowHistory) -> str | None:
    for e in history.events:
        if e.event_type == E.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED:
            value = first(e.workflow_execution_completed_event_attributes.result)
            return None if value is None else str(value)
    return None


def apply_kills(lives: list[Lifetime], kills: list[dict[str, Any]]) -> None:
    """A console SIGKILL ends the lifetime that was working before it (crash demo)."""
    for kill in kills:
        at = parse_iso(kill.get("at"))
        if at is None or kill.get("name") not in ("worker-support", "agentcore-local"):
            continue
        before = [life for life in lives if life.first <= at]
        if not before:
            continue
        victim = before[-1]
        after = [life for life in lives if life.first > at]
        if after or victim.last <= at:
            victim.end, victim.end_reason = at, "killed"
            victim.sources.add("console")


class RunService:
    def __init__(
        self,
        settings: Settings,
        gateway: Gateway,
        procs: ProcessManager,
        stack: StackProbe,
        jobs: JobStore,
        catalog: dict[str, Scenario],
        wci: WciReader | None = None,
    ) -> None:
        self.settings = settings
        self.gateway = gateway
        self.procs = procs
        self.stack = stack
        self.jobs = jobs
        self.catalog = catalog
        self.wci = wci or WciReader(gateway)
        self._static: dict[tuple[str, str], StaticInfo] = {}
        self._closed_tasks: dict[tuple[str, str], tuple[str | None, str]] = {}
        self._views: dict[str, tuple[float, dict[str, Any], list[dict[str, Any]]]] = {}
        self._rows_state: dict[str, tuple[list[tuple[Any, ...]], int]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._sticky: dict[str, set[str]] = {}
        #: Refreshes the shared stack snapshot (the stream hub's cache) before a build:
        #: compute state reads ``/ping`` and pollers from it.
        self.stack_refresh: Callable[[], Awaitable[Any]] | None = None
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    # --- console-side run records (scenario, queue, A2A mode) ------------------------------

    @property
    def runs_dir(self) -> Path:
        return self.settings.state_dir / "runs"

    def _record_path(self, workflow_id: str) -> Path:
        return self.runs_dir / f"{re.sub(r'[^A-Za-z0-9._-]', '_', workflow_id)}.json"

    def record(self, workflow_id: str) -> dict[str, Any]:
        try:
            return json.loads(self._record_path(workflow_id).read_text())
        except (OSError, ValueError):
            return {}

    def save_record(self, workflow_id: str, **fields: Any) -> None:
        data = {**self.record(workflow_id), **fields}
        self._record_path(workflow_id).write_text(json.dumps(data, indent=2))

    # --- scenario inference ----------------------------------------------------------------

    def infer_scenario(self, workflow_id: str, info: StaticInfo) -> str | None:
        recorded = self.record(workflow_id).get("scenario")
        if recorded in self.catalog:
            return recorded
        case = {"1042": "A", "2077": "B"}.get(info.order_id or "")
        if case is None:
            return None
        if info.task_queue == VERSIONED_TQ:
            if case == "A":
                return "case-a"
            snap = self.stack.last or {}
            if (snap.get("wci") or {}).get("mode") == "emulator":
                return "wci-b"
            return "serverless-b"
        return "case-a" if case == "A" else "case-b"

    # --- the run view ------------------------------------------------------------------------

    async def view(self, workflow_id: str, *, fresh: bool = False) -> dict[str, Any]:
        return (await self._get(support_id(workflow_id), fresh))[0]

    async def rows(self, workflow_id: str, after: int | None = None) -> dict[str, Any]:
        wf = support_id(workflow_id)
        view, rows = await self._get(wf, False)
        if after is not None:
            rows = [r for r in rows if r["seq"] > after]
        return {
            "workflow_id": wf,
            "t0": view["started_at"],
            "rows_version": view["rows_version"],
            "rows": rows,
        }

    def rows_delta(
        self, workflow_id: str, previous: list[tuple[Any, ...]] | None, rows: list[dict[str, Any]]
    ) -> tuple[bool, list[dict[str, Any]]]:
        """For the stream: ``(reset, new rows)`` against what a client already has."""
        keys = [_row_key(r) for r in rows]
        if previous is None or keys[: len(previous)] != previous:
            return True, rows
        return False, rows[len(previous) :]

    async def _get(self, wf: str, fresh: bool) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        lock = self._locks.setdefault(wf, asyncio.Lock())
        async with lock:
            cached = self._views.get(wf)
            if cached and not fresh and time.monotonic() - cached[0] < VIEW_TTL_S:
                return cached[1], cached[2]
            view, rows = await self._build(wf)
            self._views[wf] = (time.monotonic(), view, rows)
            return view, rows

    def _version(self, wf: str, rows: list[dict[str, Any]]) -> int:
        keys = [_row_key(r) for r in rows]
        prev = self._rows_state.get(wf)
        if prev is None:
            version = 1
        elif prev[0] == keys:
            version = prev[1]
        else:
            version = prev[1] + 1
        self._rows_state[wf] = (keys, version)
        return version

    async def _finance(self, task_id: str) -> WorkflowHistory | None:
        try:
            return await self.gateway.fetch_history(SERVICES_NS, task_id)
        except NotFound:
            return None

    async def task_now(
        self, task_id: str, facts: FinanceFacts | None
    ) -> tuple[dict[str, Any] | None, str | None]:
        """``get_task`` on a running Finance task (safe: Finance, not Support)."""
        if facts is None or facts.closed:
            return None, None
        try:
            value = await self.gateway.query(SERVICES_NS, task_id, "get_task", QUERY_TIMEOUT_S)
        except Exception:  # no Finance poller: the query times out; history answers instead
            return None, None
        return (value if isinstance(value, dict) else _as_dict(value)), "query"

    async def _build(self, wf: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        s = self.settings
        if self.stack_refresh is not None:
            with contextlib.suppress(Exception):
                await self.stack_refresh()
        desc = await self.gateway.describe(AGENTS_NS, wf)
        info_raw = desc.workflow_execution_info
        run_id = info_raw.execution.run_id
        history = await self.gateway.fetch_history(AGENTS_NS, wf, run_id)
        info = self._static.get((wf, run_id))
        if info is None:
            info = static_info(history)
            self._static[(wf, run_id)] = info
        status = _status_name(info_raw.status)
        start = info.started_at or ts(info_raw.start_time) or now()
        closed_at = ts(info_raw.close_time) if status != "RUNNING" else None
        nexus = nexus_facts(history)
        task_id = nexus.finance_ids[-1] if nexus.finance_ids else finance_task_id(wf)
        send_pair = f"nx-{nexus.send_event_id}" if nexus.send_event_id else None

        rows: list[Row] = support_rows(
            history, ui_base=s.ui_base, workflow_id=wf, finance_task_id=task_id
        )
        fin_history = await self._finance(task_id)
        facts: FinanceFacts | None = None
        if fin_history is not None:
            frows, facts = finance_rows(
                fin_history, ui_base=s.ui_base, task_id=task_id, send_pair=send_pair
            )
            rows += frows
        queried, source = await self.task_now(task_id, facts)

        # Compute: lifetimes from identities, + the app log, + console kills.
        lives = lifetimes_from_history(history)
        serverless = info.task_queue == VERSIONED_TQ
        app_events: list[AppEvent] = []
        if serverless:
            app_events = parse_app_log(tail_file(self.procs.log_path("agentcore-local"), 4000))
            apply_app_log(lives, app_events)
        apply_kills(lives, self.procs.kills())
        rows += lifetime_rows(lives, app_events)

        pending = pending_nexus(desc)
        rows += attempt_rows(pending)

        versioning = info_raw.versioning_info
        build = versioning.deployment_version.build_id or None
        wci_events: list[WciEvent] | None = None
        if serverless and build:
            with contextlib.suppress(NotFound, TemporalUnreachable):
                wci_events = await self.wci.events(build, start - timedelta(seconds=2))
        if wci_events:
            rows += wci_rows(wci_events, start, closed_at)

        final_rows = finalize(rows, start)
        rows_version = self._version(wf, final_rows)
        view = self._view(
            wf=wf,
            run_id=run_id,
            info=info,
            status=status,
            start=start,
            closed_at=closed_at,
            desc=desc,
            history=history,
            nexus=nexus,
            task_id=task_id,
            facts=facts,
            queried=queried,
            source=source,
            lives=lives,
            app_events=app_events,
            pending=pending,
            wci_events=wci_events,
            rows_version=rows_version,
        )
        return view, final_rows

    def _compute(
        self,
        *,
        serverless: bool,
        lives: list[Lifetime],
        app_events: list[AppEvent],
        closed: bool,
    ) -> dict[str, Any]:
        snap = self.stack.last or {}
        drain: dict[str, Any] = {"state": "unknown", "at": None, "estimated": False, "source": None}
        running: bool | None = None
        latest = lives[-1] if lives else None
        if not serverless:
            managed = self.procs.records.get("worker-support")
            if managed is not None:
                running = self.procs.running("worker-support")
                source = "process"
            else:
                running = bool((snap.get("pollers") or {}).get("agents/support-dev-tq"))
                source = "pollers"
            drain = {
                "state": "running" if running else "drained",
                "at": None,
                "estimated": False,
                "source": source,
            }
        else:
            ping = (snap.get("agentcore_app") or {}).get("status")
            log_end = latest.end if (latest and latest.end_reason == "drained") else None
            if ping == "HealthyBusy":
                running = True
                drain = {"state": "running", "at": None, "estimated": False, "source": "ping"}
            elif log_end is not None:
                running = False
                drain = {
                    "state": "drained",
                    "at": iso(log_end),
                    "estimated": False,
                    "source": "app-log",
                }
            elif ping == "Healthy" and latest is not None:
                running = False
                drain = {"state": "drained", "at": None, "estimated": False, "source": "ping"}
            elif ping is None and latest is not None and app_events == []:
                running = False
                drain = {"state": "drained", "at": None, "estimated": True, "source": "no-app"}
        if closed and running is None:
            running = False
        lifetimes = []
        for n, life in enumerate(lives, 1):
            nxt = lives[n].first if n < len(lives) else None
            end = life.end
            reason = life.end_reason
            if end is None and drain["state"] == "drained" and life is latest:
                if drain["at"]:
                    end = parse_iso(drain["at"])
                elif not serverless:
                    end = self._plain_end(life, None) or life.last
                else:
                    end = life.last
                reason = reason or ("drained" if serverless else "stopped")
            if end is None and latest is not None and life is not latest:
                end = (None if serverless else self._plain_end(life, nxt)) or life.last
                reason = reason or "ended"
            lifetimes.append(
                {
                    "n": n,
                    "label": lifetime_label(n, life),
                    "identity": life.identity,
                    "start": iso(life.first),
                    "end": iso(end),
                    "end_reason": reason,
                    "workflow_tasks": life.workflow_tasks,
                    "activities": life.activities,
                    "source": "+".join(sorted(life.sources)),
                }
            )
        return {"running": running, "drain": drain, "lifetimes": lifetimes}

    def _plain_end(self, life: Lifetime, next_start: datetime | None) -> datetime | None:
        """When a plain Support worker stopped: its process exit, else its last poll.

        Its last history event is only when it last worked; a worker killed while Finance
        waits was up (and polling) until the kill.
        """
        exits = []
        for e in self.procs.exits("worker-support"):
            at, started = parse_iso(e.get("at")), parse_iso(e.get("started_at"))
            if at is None or at < life.last or (started is not None and started > life.first):
                continue
            if next_start is not None and at > next_start + timedelta(seconds=5):
                continue
            exits.append(at)
        if exits:
            return min(exits)
        seen = self.stack.poller_seen.get(life.identity)
        if seen is not None and seen >= life.last and (next_start is None or seen <= next_start):
            return seen
        return None

    def _view(
        self,
        *,
        wf: str,
        run_id: str,
        info: StaticInfo,
        status: str,
        start: datetime,
        closed_at: datetime | None,
        desc: DescribeWorkflowExecutionResponse,
        history: WorkflowHistory,
        nexus: _Nexus,
        task_id: str,
        facts: FinanceFacts | None,
        queried: dict[str, Any] | None,
        source: str | None,
        lives: list[Lifetime],
        app_events: list[AppEvent],
        pending: list[dict[str, Any]],
        wci_events: list[WciEvent] | None,
        rows_version: int,
    ) -> dict[str, Any]:
        s = self.settings
        serverless = info.task_queue == VERSIONED_TQ
        scenario_id = self.infer_scenario(wf, info)
        scenario = self.catalog.get(scenario_id or "")
        versioning = desc.workflow_execution_info.versioning_info
        dv = versioning.deployment_version

        # The A2A task.
        task: dict[str, Any] = {
            "id": task_id,
            "state": None,
            "source": None,
            "message": None,
            "updated_at": None,
            "approval": None,
            "artifact": None,
        }
        if queried is not None:
            state, text, artifact = a2a_state_from_result(queried)
            task.update(
                state=state,
                source="query",
                message=text or None,
                updated_at=(queried.get("status") or {}).get("timestamp"),
                artifact=artifact,
            )
        elif facts is not None:
            task.update(
                state=facts.state,
                source="history",
                message=facts.message or None,
                updated_at=iso(facts.updated_at),
                artifact=facts.artifact,
            )
        if facts is not None and facts.timer_started and facts.approval_timeout_s:
            deadline = facts.timer_started + timedelta(seconds=facts.approval_timeout_s)
            task["approval"] = {
                "timeout_text": _dur_text(facts.approval_timeout_s),
                "deadline": iso(deadline),
            }
        if task["updated_at"]:
            parsed = parse_iso(task["updated_at"])
            task["updated_at"] = iso(parsed) if parsed else task["updated_at"]

        compute = self._compute(
            serverless=serverless,
            lives=lives,
            app_events=app_events,
            closed=status != "RUNNING",
        )
        invokes = sum(
            1
            for ev in (wci_events or [])
            if ev.kind == "wci.invoke"
            and ev.at >= start - timedelta(seconds=2)
            and (closed_at is None or ev.at <= closed_at + timedelta(seconds=2))
        )
        wci = {"invokes": invokes, "rows_source": "wci-history" if wci_events else None}

        # Reply.
        reply = None
        if status == "COMPLETED":
            artifact = task.get("artifact") or {}
            reply = {"text": reply_text(history) or "", "refund_id": artifact.get("refund_id")}

        # Metrics.
        end_hold = nexus.send_ended or (None if status == "RUNNING" else closed_at) or now()
        held = seconds(nexus.send_scheduled, end_hold) if nexus.send_scheduled else None
        approval_wait = None
        approval_to_reply = None
        if facts is not None and facts.timer_started:
            approval_wait = seconds(facts.timer_started, facts.signal_at or now())
            if facts.signal_at and closed_at and status == "COMPLETED":
                approval_to_reply = seconds(facts.signal_at, closed_at)
        builds, _behaviors = build_ids(history)
        if dv.build_id and dv.build_id not in builds:
            builds.append(dv.build_id)
        q15 = self._q15(history, nexus, wci_events, closed_at) if serverless else None

        ctx = StepContext(
            run_started=True,
            run_closed=status != "RUNNING",
            run_status=status,
            task_state=task["state"],
            lifetimes=len(lives),
            drain=compute["drain"]["state"],
            running=compute["running"],
            nexus_attempt=max(
                (p["attempt"] for p in pending if p["operation"] == "send_message"), default=0
            ),
            nexus_completed=nexus.send_outcome == "completed",
            card_discovered=nexus.card_discovered,
            wci_invokes=invokes,
            processes={p["name"]: p["status"] for p in self.procs.describe_all()},
            jobs=self._job_states(),
        )
        steps: list[dict[str, Any]] = []
        if scenario is not None:
            seen = self._sticky.setdefault(f"{wf}/{run_id}", set())
            steps = step_statuses(scenario.steps, ctx, self._blocked(scenario, task["state"]), seen)
            seen.update(st["id"] for st in steps if st["status"] == "done")

        amount = nexus.send_amount or ORDER_AMOUNTS.get(info.order_id or "")
        return {
            "workflow_id": wf,
            "run_id": run_id,
            "scenario": scenario_id,
            "case": {"1042": "A", "2077": "B"}.get(info.order_id or ""),
            "order_id": info.order_id,
            "amount_usd": amount,
            "ticket": info.ticket,
            "a2a_mode": info.a2a_mode,
            "nexus_send_timeout_text": info.budget or None,
            "task_queue": info.task_queue,
            "status": status,
            "started_at": iso(start),
            "closed_at": iso(closed_at),
            "versioning": {
                "deployment": dv.deployment_name or None,
                "build_id": dv.build_id or None,
                "behavior": _behavior(versioning.behavior),
            },
            "support": {
                "pending_nexus": public(pending),
                "agent_card_discovered": nexus.card_discovered,
            },
            "task": task,
            "compute": compute,
            "wci": wci,
            "reply": reply,
            "metrics": {
                "held_open_s": held,
                "approval_wait_s": approval_wait,
                "approval_to_reply_s": approval_to_reply,
                "lifetimes": len(lives),
                "build_ids": builds,
                "q15": q15,
            },
            "steps": steps,
            "links": {
                "support": ui_link(s.ui_base, AGENTS_NS, wf, run_id),
                "finance": ui_link(s.ui_base, SERVICES_NS, task_id),
                "nexus_endpoints": f"{s.ui_base}/nexus",
            },
            "rows_version": rows_version,
        }

    def _blocked(self, scenario: Scenario, task_state: str | None) -> dict[str, str]:
        """Why a step's action can't run now (only shown when it is the current step)."""
        out: dict[str, str] = {}
        build = self.stack.facts.build_id
        for step in scenario.steps:
            if step.action == "approve" and task_state not in ("input-required",):
                out[step.id] = (
                    f"The A2A task is {task_state or 'not started yet'}; "
                    "approve and reject only apply to input-required."
                )
            elif step.action == "invoke-local":
                if not build:
                    out[step.id] = (
                        "No Build ID. Commit uv.lock, or start with TEMPORAL_BUILD_ID=<id>."
                    )
                elif not self.stack.facts.agentcore_up:
                    out[step.id] = "The AgentCore app is not running on :8080."
            elif step.action in ("process.start", "process.stop"):
                name = step.params.get("name", "")
                proc = self.procs.describe(name) if name in self._proc_names() else None
                if proc is None:
                    out[step.id] = f"No process {name!r}."
                elif proc["status"] == "unavailable":
                    out[step.id] = proc.get("why") or "Not available on this profile."
                elif step.action == "process.stop" and proc["status"] == "external":
                    out[step.id] = f"{proc['label']} was started outside the console."
        return out

    @staticmethod
    def _proc_names() -> set[str]:
        return set(SPECS)

    def _job_states(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for job in sorted(self.jobs.jobs.values(), key=lambda j: j.started_at):
            out[job.action] = job.status
        return out

    @staticmethod
    def _q15(
        history: WorkflowHistory,
        nexus: _Nexus,
        wci_events: list[WciEvent] | None,
        closed_at: datetime | None,
    ) -> dict[str, float | None] | None:
        done = nexus.send_ended if nexus.send_outcome == "completed" else None
        if done is None:
            return None

        # Only this run's WCI activity: a later run on the same build (or a WCI started after
        # an invoke-local run) must not show up as this run's InvokeWorker.
        hi = closed_at + timedelta(seconds=2) if closed_at else None

        def after(kind: str) -> datetime | None:
            return next(
                (
                    ev.at
                    for ev in wci_events or []
                    if ev.kind == kind and ev.at >= done and (hi is None or ev.at <= hi)
                ),
                None,
            )

        first_wft = first_after(history, E.EVENT_TYPE_WORKFLOW_TASK_STARTED, done)
        return {
            "task_add_signal_s": seconds(done, after("wci.signal")),
            "invoke_worker_s": seconds(done, after("wci.invoke")),
            "first_wft_s": seconds(done, first_wft),
            "reply_s": seconds(done, closed_at),
        }

    # --- the report (approval_report.py + serverless_report.py) -------------------------------

    async def report(self, workflow_id: str) -> dict[str, Any]:
        wf = support_id(workflow_id)
        view, _ = await self._get(wf, False)
        history = await self.gateway.fetch_history(AGENTS_NS, wf, view["run_id"])
        events = list(history.events)
        nexus = nexus_facts(history)
        send: dict[str, Any] | None = None
        during: dict[str, Any] | None = None
        if nexus.send_scheduled is not None:
            end = nexus.send_ended
            send = {
                "scheduled": iso(nexus.send_scheduled),
                "started": iso(nexus.send_started),
                "ended": iso(end),
                "outcome": nexus.send_outcome or "pending",
                "budget_s": nexus.send_budget_s,
                "open_s": seconds(nexus.send_scheduled, end or now()),
            }
            lo = nexus.send_started or nexus.send_scheduled
            hi = end or now()
            window = [e for e in events if (t := ts(e.event_time)) is not None and lo <= t <= hi]
            wfts = [e for e in window if e.event_type == E.EVENT_TYPE_WORKFLOW_TASK_STARTED]
            times = [ts(e.event_time) for e in window]
            gaps = [
                (b - a).total_seconds()
                for a, b in itertools.pairwise(times)
                if a is not None and b is not None
            ]
            if end is None:
                last = times[-1] if times else lo
                if last is not None:
                    gaps.append((now() - last).total_seconds())
            idents = [
                e.workflow_task_started_event_attributes.identity
                for e in events
                if e.event_type == E.EVENT_TYPE_WORKFLOW_TASK_STARTED
            ]
            before = [
                e.workflow_task_started_event_attributes.identity
                for e in events
                if e.event_type == E.EVENT_TYPE_WORKFLOW_TASK_STARTED
                and (t := ts(e.event_time)) is not None
                and t < lo
            ]
            after = [
                e.workflow_task_started_event_attributes.identity
                for e in events
                if e.event_type == E.EVENT_TYPE_WORKFLOW_TASK_STARTED
                and end is not None
                and (t := ts(e.event_time)) is not None
                and t > end
            ]
            during = {
                "workflow_tasks": len(wfts),
                "longest_silence_s": round(max(gaps, default=0.0), 3),
                "identities": len(set(idents)),
                "same_worker_after": bool(before and after and before[-1] == after[0]),
            }
        finance: dict[str, Any] | None = None
        fin_history = await self._finance(view["task"]["id"])
        if fin_history is not None:
            _, facts = finance_rows(
                fin_history,
                ui_base=self.settings.ui_base,
                task_id=view["task"]["id"],
                send_pair=None,
            )
            finance = {
                "timer_started": iso(facts.timer_started),
                "timer": facts.timer_outcome or ("running" if facts.timer_started else None),
                "signal": facts.signal,
                "final_state": view["task"]["state"],
            }
        lives = lifetimes_from_history(history)
        builds, behaviors = build_ids(history)
        return {
            "send_message": send,
            "support_during_wait": during,
            "finance": finance,
            "lifetimes": [
                {
                    "identity": life.identity,
                    "first": iso(life.first),
                    "last": iso(life.last),
                    "wft": life.workflow_tasks,
                    "activities": life.activities,
                }
                for life in lives
            ],
            "build_ids": builds,
            "behaviors": behaviors,
            "q15": view["metrics"]["q15"],
        }

    # --- the list ------------------------------------------------------------------------------

    async def list_runs(self, limit: int = 25) -> dict[str, Any]:
        support = await self.gateway.list_workflows(
            AGENTS_NS, f"WorkflowType='{SUPPORT_TYPE}'", limit
        )
        tasks = await self.gateway.list_workflows(
            SERVICES_NS, f"WorkflowType='{FINANCE_TYPE}'", max(limit * 2, 50)
        )
        task_by_id = {t.workflow_id: t for t in tasks}
        loads = await self.gateway.list_workflows(AGENTS_NS, "WorkflowId STARTS_WITH 'load-'", 200)

        async def one(ex: Any) -> dict[str, Any]:
            info = self._static.get((ex.workflow_id, ex.run_id))
            if info is None:
                with contextlib.suppress(NotFound):
                    hist = await self.gateway.fetch_history(AGENTS_NS, ex.workflow_id, ex.run_id)
                    info = static_info(hist)
                    self._static[(ex.workflow_id, ex.run_id)] = info
            task_state, task_source = await self._task_state(
                task_by_id.get(finance_task_id(ex.workflow_id))
            )
            group = _LOAD.match(ex.workflow_id)
            order = info.order_id if info else None
            return {
                "workflow_id": ex.workflow_id,
                "run_id": ex.run_id,
                "scenario": self.infer_scenario(ex.workflow_id, info) if info else None,
                "case": {"1042": "A", "2077": "B"}.get(order or ""),
                "status": ex.status,
                "task_state": task_state,
                "task_state_source": task_source,
                "task_queue": ex.task_queue,
                "build_id": getattr(ex, "build_id", None),
                "amount_usd": ORDER_AMOUNTS.get(order or ""),
                "started_at": ex.start_time,
                "closed_at": ex.close_time,
                "group": group.group(1) if group else None,
            }

        runs = list(await asyncio.gather(*(one(ex) for ex in support)))
        groups: dict[str, dict[str, Any]] = {}
        for ex in loads:
            m = _LOAD.match(ex.workflow_id)
            if not m:
                continue
            g = groups.setdefault(
                m.group(1),
                {"id": m.group(1), "kind": None, "n": 0, "completed": 0, "failed": 0, "running": 0},
            )
            g["kind"] = "ping" if ex.workflow_type == "PingWorkflow" else "case-a"
            g["n"] += 1
            if ex.status == "COMPLETED":
                g["completed"] += 1
            elif ex.status == "RUNNING":
                g["running"] += 1
            else:
                g["failed"] += 1
        return {
            "runs": runs,
            "groups": sorted(groups.values(), key=lambda g: g["id"], reverse=True),
            "next_page_token": None,
        }

    async def _task_state(self, task: Any) -> tuple[str | None, str | None]:
        if task is None:
            return None, None
        cached = self._closed_tasks.get((task.workflow_id, task.run_id))
        if cached is not None:
            return cached[0], "cache"
        try:
            hist = await self.gateway.fetch_history(SERVICES_NS, task.workflow_id, task.run_id)
        except NotFound:
            return None, None
        _, facts = finance_rows(hist, ui_base="", task_id=task.workflow_id, send_pair=None)
        if task.status != "RUNNING":
            self._closed_tasks[(task.workflow_id, task.run_id)] = (facts.state, "history")
        return facts.state, "history"

    def forget(self, workflow_id: str) -> None:
        self._views.pop(support_id(workflow_id), None)


def lifetime_label(n: int, life: Lifetime) -> str:
    """``lifetime 2`` per run; the app's own number when it differs (its identity's suffix)."""
    if life.agentcore_n is None:
        return f"worker {life.pid}"
    if life.agentcore_n == n:
        return f"lifetime {n}"
    return f"lifetime {n} (app lifetime {life.agentcore_n})"


def _row_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (row.get("at"), row.get("lane"), row.get("kind"), row.get("title"))


def _dur_text(seconds_value: float) -> str:
    total = round(seconds_value)
    if total % 3600 == 0:
        return f"{total // 3600}h"
    if total % 60 == 0:
        return f"{total // 60}m"
    return f"{total}s"


def _as_dict(value: Any) -> dict[str, Any] | None:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    return None
