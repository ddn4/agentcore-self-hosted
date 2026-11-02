"""Lane rows for the run view (DESIGN.md 5.3 ``rows``): both histories, the WCI, lifetimes.

Every row is derived from Temporal (Support's history in ``agents``, the A2A task's history in
``agent-services``, the WCI system Workflow) or from the console's own laptop sources (the
AgentCore app's log, process kills). Pure functions; ``seq`` is the position after a stable
sort by time, then lane order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from temporalio.api.enums.v1 import EventType
from temporalio.api.history.v1 import HistoryEvent
from temporalio.client import WorkflowHistory

from demo_console.lifetimes import AppEvent, Lifetime, WciEvent, app_identities
from demo_console.payloads import decode, first, iso, summary, ts

E = EventType
LANE_ORDER = {"compute": 0, "wci": 1, "support": 2, "nexus": 3, "finance": 4, "human": 5}
ACTIVITY_TITLES = {
    "lookup_order": "Looks up the order",
    "get_return_policy": "Reads the return policy",
    "fraud_score": "Scores fraud risk",
    "refund_history": "Reads refund history",
    "issue_refund": "Issues the refund",
}
NEXUS_TITLES = {
    "get_agent_card": "Discovers Finance's Agent Card",
    "send_message": "Asks Finance for a refund",
}


@dataclass
class Row:
    at: datetime
    lane: str
    kind: str
    title: str
    extra: dict[str, Any] = field(default_factory=dict)

    def key(self) -> tuple[str, str, str, str]:
        return (iso(self.at) or "", self.lane, self.kind, self.title)


def ui_link(ui_base: str, namespace: str, workflow_id: str, run_id: str | None = None) -> str:
    base = f"{ui_base}/namespaces/{namespace}/workflows/{workflow_id}"
    return f"{base}/{run_id}/history" if run_id else base


def _agent_label(summary_text: str) -> str:
    return summary_text or "Agent"


def a2a_state_from_result(value: Any) -> tuple[str | None, str, dict[str, Any] | None]:
    """``(state, message text, refund_confirmation data)`` from an A2ATask JSON value."""
    if not isinstance(value, dict):
        return None, "", None
    status = value.get("status") or {}
    state = status.get("state")
    text = ""
    message = status.get("message") or {}
    for part in message.get("parts") or []:
        if part.get("kind") == "text" and part.get("text"):
            text = part["text"]
            break
    artifact = None
    for art in value.get("artifacts") or []:
        if art.get("name") == "refund_confirmation":
            for part in art.get("parts") or []:
                if part.get("kind") == "data" and isinstance(part.get("data"), dict):
                    artifact = part["data"]
                    break
    return state, text, artifact


def support_rows(
    history: WorkflowHistory, *, ui_base: str, workflow_id: str, finance_task_id: str
) -> list[Row]:
    rows: list[Row] = []
    ops: dict[int, str] = {}
    activity_types: dict[int, tuple[str, str]] = {}
    activity_rows: dict[int, Row] = {}
    last_wft_identity: str | None = None
    support_link = ui_link(ui_base, "agents", workflow_id)
    finance_link = ui_link(ui_base, "agent-services", finance_task_id)
    for e in history.events:
        at = ts(e.event_time)
        if at is None:
            continue
        et = e.event_type
        base = {"event_id": e.event_id}
        if et == E.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED:
            ticket = first(e.workflow_execution_started_event_attributes.input)
            rows.append(
                Row(
                    at,
                    "support",
                    "run.started",
                    "Ticket received",
                    {**base, "detail": str(ticket or ""), "link": support_link},
                )
            )
        elif et == E.EVENT_TYPE_WORKFLOW_TASK_STARTED:
            identity = e.workflow_task_started_event_attributes.identity
            if identity and identity != last_wft_identity:
                rows.append(
                    Row(
                        at,
                        "support",
                        "wft",
                        "Workflow Task on a new worker"
                        if last_wft_identity
                        else "First Workflow Task",
                        {**base, "identity": identity},
                    )
                )
                last_wft_identity = identity
        elif et == E.EVENT_TYPE_WORKFLOW_TASK_TIMED_OUT:
            rows.append(
                Row(
                    at,
                    "support",
                    "wft",
                    "Workflow Task timed out (worker gone)",
                    {**base, "detail": "the server hands it to the next worker"},
                )
            )
        elif et == E.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            attrs = e.activity_task_scheduled_event_attributes
            name = attrs.activity_type.name
            label = summary(e.user_metadata.summary)
            activity_types[e.event_id] = (name, label)
            if name == "invoke_model":
                agent = _agent_label(label)
                row = Row(
                    at,
                    "support",
                    "model.turn",
                    f"{agent} thinks",
                    {**base, "agent": agent},
                )
            else:
                row = Row(
                    at,
                    "support",
                    "activity",
                    ACTIVITY_TITLES.get(name, name),
                    {**base, "detail": label or name},
                )
            activity_rows[e.event_id] = row
            rows.append(row)
        elif et == E.EVENT_TYPE_ACTIVITY_TASK_STARTED:
            attrs = e.activity_task_started_event_attributes
            row = activity_rows.get(attrs.scheduled_event_id)
            if row is not None:
                row.extra["identity"] = attrs.identity
                if attrs.attempt > 1:
                    row.extra["attempt"] = attrs.attempt
        elif et == E.EVENT_TYPE_ACTIVITY_TASK_TIMED_OUT:
            sid = e.activity_task_timed_out_event_attributes.scheduled_event_id
            name, label = activity_types.get(sid, ("activity", ""))
            what = f"{label} model turn" if name == "invoke_model" else name
            rows.append(
                Row(
                    at,
                    "support",
                    "activity",
                    f"{what} timed out: its worker is gone",
                    {**base, "detail": "retried on the next worker"},
                )
            )
        elif et == E.EVENT_TYPE_NEXUS_OPERATION_SCHEDULED:
            attrs = e.nexus_operation_scheduled_event_attributes
            ops[e.event_id] = attrs.operation
            label = summary(e.user_metadata.summary)
            rows.append(
                Row(
                    at,
                    "nexus",
                    "nexus.scheduled",
                    NEXUS_TITLES.get(attrs.operation, attrs.operation),
                    {
                        **base,
                        "detail": label or f"{attrs.service}.{attrs.operation}",
                        "operation": attrs.operation,
                        "pair": f"nx-{e.event_id}",
                        "from": "support",
                        "to": "finance",
                    },
                )
            )
        elif et == E.EVENT_TYPE_NEXUS_OPERATION_STARTED:
            sid = e.nexus_operation_started_event_attributes.scheduled_event_id
            rows.append(
                Row(
                    at,
                    "nexus",
                    "nexus.started",
                    "Finance accepted the task",
                    {
                        **base,
                        "operation": ops.get(sid, ""),
                        "detail": "async operation: the server holds the call open",
                        "link": finance_link,
                    },
                )
            )
        elif et == E.EVENT_TYPE_NEXUS_OPERATION_COMPLETED:
            attrs = e.nexus_operation_completed_event_attributes
            op = ops.get(attrs.scheduled_event_id, "")
            value = decode(attrs.result)
            if op == "send_message":
                state, text, _ = a2a_state_from_result(value)
                title = f"Finance answers: {state or 'done'}"
                extra = {
                    "pair": f"nx-{attrs.scheduled_event_id}-back",
                    "from": "finance",
                    "to": "support",
                    "state": state,
                    "detail": text,
                }
            else:
                title = "Agent Card received"
                name = value.get("name") if isinstance(value, dict) else None
                extra = {
                    "pair": f"nx-{attrs.scheduled_event_id}",
                    "from": "finance",
                    "to": "support",
                    "detail": f"{name}" if name else "",
                }
            rows.append(
                Row(at, "nexus", "nexus.completed", title, {**base, "operation": op, **extra})
            )
        elif et in (
            E.EVENT_TYPE_NEXUS_OPERATION_FAILED,
            E.EVENT_TYPE_NEXUS_OPERATION_TIMED_OUT,
            E.EVENT_TYPE_NEXUS_OPERATION_CANCELED,
        ):
            attrs = (
                e.nexus_operation_failed_event_attributes
                if et == E.EVENT_TYPE_NEXUS_OPERATION_FAILED
                else e.nexus_operation_timed_out_event_attributes
                if et == E.EVENT_TYPE_NEXUS_OPERATION_TIMED_OUT
                else e.nexus_operation_canceled_event_attributes
            )
            op = ops.get(attrs.scheduled_event_id, "")
            what = {
                E.EVENT_TYPE_NEXUS_OPERATION_FAILED: "failed",
                E.EVENT_TYPE_NEXUS_OPERATION_TIMED_OUT: "timed out",
                E.EVENT_TYPE_NEXUS_OPERATION_CANCELED: "canceled",
            }[et]
            rows.append(
                Row(
                    at,
                    "nexus",
                    "nexus.failed",
                    f"{op} {what}",
                    {
                        **base,
                        "operation": op,
                        "detail": attrs.failure.message[:200],
                        "pair": f"nx-{attrs.scheduled_event_id}-back",
                        "from": "finance",
                        "to": "support",
                    },
                )
            )
        elif et == E.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED:
            reply = first(e.workflow_execution_completed_event_attributes.result)
            rows.append(
                Row(
                    at,
                    "support",
                    "run.completed",
                    "Support replies to the customer",
                    {**base, "detail": str(reply or "")},
                )
            )
        elif et in (
            E.EVENT_TYPE_WORKFLOW_EXECUTION_FAILED,
            E.EVENT_TYPE_WORKFLOW_EXECUTION_TIMED_OUT,
        ):
            msg = (
                e.workflow_execution_failed_event_attributes.failure.message
                if et == E.EVENT_TYPE_WORKFLOW_EXECUTION_FAILED
                else "timed out"
            )
            rows.append(
                Row(at, "support", "run.failed", "The conversation failed", {**base, "detail": msg})
            )
        elif et in (
            E.EVENT_TYPE_WORKFLOW_EXECUTION_TERMINATED,
            E.EVENT_TYPE_WORKFLOW_EXECUTION_CANCELED,
        ):
            reason = (
                e.workflow_execution_terminated_event_attributes.reason
                if et == E.EVENT_TYPE_WORKFLOW_EXECUTION_TERMINATED
                else "canceled"
            )
            rows.append(
                Row(
                    at,
                    "support",
                    "run.terminated",
                    "The conversation was ended",
                    {**base, "detail": reason},
                )
            )
    return rows


@dataclass
class FinanceFacts:
    """What Finance's history says about the A2A task (no query)."""

    state: str | None = None
    message: str = ""
    updated_at: datetime | None = None
    approval_timeout_s: float | None = None
    timer_started: datetime | None = None
    timer_outcome: str | None = None  # fired | canceled
    signal: dict[str, Any] | None = None
    signal_at: datetime | None = None
    artifact: dict[str, Any] | None = None
    closed: bool = False
    started_at: datetime | None = None


def finance_rows(
    history: WorkflowHistory, *, ui_base: str, task_id: str, send_pair: str | None
) -> tuple[list[Row], FinanceFacts]:
    """The A2A task's rows, and its state inferred from history (``source: "history"``)."""
    rows: list[Row] = []
    facts = FinanceFacts()
    link = ui_link(ui_base, "agent-services", task_id)
    activity_rows: dict[int, Row] = {}
    activity_names: dict[int, str] = {}
    first_wft_done = False
    for e in history.events:
        at = ts(e.event_time)
        if at is None:
            continue
        et = e.event_type
        base = {"event_id": e.event_id}
        if et == E.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED:
            facts.state, facts.updated_at, facts.started_at = "submitted", at, at
            extra: dict[str, Any] = {**base, "state": "submitted", "link": link}
            if send_pair:
                extra["pair"] = send_pair
            rows.append(Row(at, "finance", "task.started", "A2A task started", extra))
        elif et == E.EVENT_TYPE_WORKFLOW_TASK_COMPLETED and not first_wft_done:
            first_wft_done = True
            if facts.state == "submitted":
                facts.state, facts.updated_at = "working", at
                rows.append(
                    Row(
                        at,
                        "finance",
                        "task.state",
                        "Finance's agent assesses the refund",
                        {**base, "state": "working"},
                    )
                )
        elif et == E.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            attrs = e.activity_task_scheduled_event_attributes
            name = attrs.activity_type.name
            label = summary(e.user_metadata.summary)
            activity_names[e.event_id] = name
            if name == "invoke_model":
                row = Row(
                    at,
                    "finance",
                    "model.turn",
                    f"{label or 'Finance Refund agent'} thinks",
                    {**base, "agent": label or "Finance Refund agent"},
                )
            else:
                row = Row(
                    at,
                    "finance",
                    "activity",
                    ACTIVITY_TITLES.get(name, name),
                    {**base, "detail": label or name},
                )
            activity_rows[e.event_id] = row
            rows.append(row)
        elif et == E.EVENT_TYPE_ACTIVITY_TASK_STARTED:
            attrs = e.activity_task_started_event_attributes
            row = activity_rows.get(attrs.scheduled_event_id)
            if row is not None:
                row.extra["identity"] = attrs.identity
                if attrs.attempt > 1:
                    row.extra["attempt"] = attrs.attempt
        elif et == E.EVENT_TYPE_ACTIVITY_TASK_COMPLETED:
            attrs = e.activity_task_completed_event_attributes
            if activity_names.get(attrs.scheduled_event_id) == "issue_refund":
                receipt = first(attrs.result)
                refund_id = receipt.get("refund_id") if isinstance(receipt, dict) else None
                rows.append(
                    Row(
                        at,
                        "finance",
                        "refund.issued",
                        f"Refund {refund_id} issued" if refund_id else "Refund issued",
                        {**base, "refund_id": refund_id},
                    )
                )
        elif et == E.EVENT_TYPE_TIMER_STARTED:
            attrs = e.timer_started_event_attributes
            seconds = attrs.start_to_fire_timeout.ToTimedelta().total_seconds()
            facts.approval_timeout_s = seconds
            facts.timer_started = at
            facts.state, facts.updated_at = "input-required", at
            rows.append(
                Row(
                    at,
                    "finance",
                    "task.state",
                    "Finance asks a human",
                    {
                        **base,
                        "state": "input-required",
                        "detail": f"Approval timer started ({_duration_text(seconds)})",
                    },
                )
            )
        elif et == E.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED:
            attrs = e.workflow_execution_signaled_event_attributes
            name = attrs.signal_name
            payload = first(attrs.input)
            approver = payload.get("approver", "") if isinstance(payload, dict) else ""
            comment = payload.get("comment", "") if isinstance(payload, dict) else ""
            effective = (
                facts.state == "input-required" and facts.signal is None and bool(approver.strip())
            )
            if name in ("approve", "reject"):
                verb = "approved" if name == "approve" else "rejected"
                title = f"Refund {verb} by {approver or '(no approver)'}"
                if not effective:
                    title += " (ignored)"
                rows.append(
                    Row(
                        at,
                        "human",
                        f"signal.{name}",
                        title,
                        {**base, "approver": approver, "detail": comment, "effective": effective},
                    )
                )
                if effective:
                    facts.signal = {"verdict": name, "approver": approver, "comment": comment}
                    facts.signal_at = at
                    facts.state = "working" if name == "approve" else "rejected"
                    facts.updated_at = at
        elif et == E.EVENT_TYPE_TIMER_FIRED:
            facts.timer_outcome = "fired"
            rows.append(Row(at, "finance", "timer.fired", "Nobody decided in time", {**base}))
        elif et == E.EVENT_TYPE_TIMER_CANCELED:
            facts.timer_outcome = "canceled"
        elif et == E.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED:
            value = first(e.workflow_execution_completed_event_attributes.result)
            state, text, artifact = a2a_state_from_result(value)
            facts.state, facts.message, facts.artifact = state, text, artifact
            facts.updated_at, facts.closed = at, True
            extra = {**base, "state": state, "detail": text}
            if send_pair:
                extra["pair"] = f"{send_pair}-back"
            rows.append(Row(at, "finance", "task.state", _final_title(state), extra))
        elif et in (
            E.EVENT_TYPE_WORKFLOW_EXECUTION_FAILED,
            E.EVENT_TYPE_WORKFLOW_EXECUTION_TERMINATED,
            E.EVENT_TYPE_WORKFLOW_EXECUTION_TIMED_OUT,
            E.EVENT_TYPE_WORKFLOW_EXECUTION_CANCELED,
        ):
            facts.state, facts.updated_at, facts.closed = (
                ("canceled" if et == E.EVENT_TYPE_WORKFLOW_EXECUTION_CANCELED else "failed"),
                at,
                True,
            )
            extra = {**base, "state": facts.state}
            if send_pair:
                extra["pair"] = f"{send_pair}-back"
            rows.append(Row(at, "finance", "task.state", _final_title(facts.state), extra))
    return rows, facts


def _final_title(state: str | None) -> str:
    return {
        "completed": "Task completed",
        "rejected": "Task rejected",
        "failed": "Task failed",
        "canceled": "Task canceled",
    }.get(state or "", f"Task {state}")


def _duration_text(seconds: float) -> str:
    seconds = round(seconds)
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def lifetime_rows(lives: list[Lifetime], app_events: list[AppEvent]) -> list[Row]:
    """Compute-rail rows: each lifetime's start (history), kill (console), app-log markers."""
    rows: list[Row] = []
    by_identity = {life.identity: (i + 1, life) for i, life in enumerate(lives)}
    for n, life in by_identity.values():
        if life.agentcore_n is None:
            title = f"Worker {life.pid} starts"
        elif life.agentcore_n == n:
            title = f"Lifetime {n} starts"
        else:
            # Per run, like the steps; the app (and the identity below) count per process.
            title = f"Lifetime {n} starts (app lifetime {life.agentcore_n})"
        rows.append(
            Row(
                life.first,
                "compute",
                "lifetime.start",
                title,
                {"lifetime": n, "identity": life.identity, "source": "history"},
            )
        )
        if life.end_reason == "killed" and life.end is not None:
            rows.append(
                Row(
                    life.end,
                    "compute",
                    "lifetime.killed",
                    f"Worker {life.pid} killed (SIGKILL)",
                    {"lifetime": n, "identity": life.identity, "source": "console"},
                )
            )
    # App-log markers for the lifetimes of this run (matched by identity).
    ids = app_identities(app_events)
    titles = {
        "connected": ("lifetime.connected", "Lifetime {n} connected"),
        "idle": ("lifetime.idle", "Lifetime {n} is idle: draining"),
        "drained": ("lifetime.drained", "Lifetime {n} drained"),
        "released": ("lifetime.released", "Lifetime {n} released its async task"),
    }
    for ev in app_events:
        if ev.marker not in titles or ev.lifetime is None:
            continue
        identity = ids.get(ev.lifetime)
        if identity not in by_identity:
            continue
        n, _ = by_identity[identity]
        kind, title = titles[ev.marker]
        rows.append(
            Row(
                ev.at,
                "compute",
                kind,
                title.format(n=n),
                {"lifetime": n, "identity": identity, "source": "app-log"},
            )
        )
    return rows


def wci_rows(events: list[WciEvent], start: datetime, end: datetime | None) -> list[Row]:
    lo = start - timedelta(seconds=2)
    hi = (end + timedelta(seconds=2)) if end else None
    titles = {
        "wci.signal": "Matching tells the WCI: {d}",
        "wci.invoke": "Server invokes AgentCore",
        "wci.failed": "WCI activity failed",
    }
    rows: list[Row] = []
    for ev in events:
        if ev.at < lo or (hi is not None and ev.at > hi):
            continue
        detail = "InvokeWorker (WCI)" if ev.kind == "wci.invoke" else ev.detail
        rows.append(
            Row(
                ev.at,
                "wci",
                ev.kind,
                titles[ev.kind].format(d=ev.detail),
                {"detail": detail, "source": "wci-history"},
            )
        )
    return rows


def attempt_rows(pending: list[dict[str, Any]]) -> list[Row]:
    """``nexus.attempt_failed`` from describe's pending Nexus operations (not in history)."""
    rows: list[Row] = []
    for op in pending:
        if op.get("attempt", 0) > 1 or op.get("last_failure"):
            when = op.get("_last_attempt_at") or op.get("_scheduled_at")
            if when is None:
                continue
            # `attempt` is the one that failed (describe's attempt is the next one); the page
            # labels the seam stub "attempt N failed" from it.
            failed = max(op.get("attempt", 1) - 1, 1)
            rows.append(
                Row(
                    when,
                    "nexus",
                    "nexus.attempt_failed",
                    f"{op['operation']} attempt {failed} failed",
                    {
                        "operation": op["operation"],
                        "attempt": failed,
                        "detail": op.get("last_failure") or "",
                    },
                )
            )
    return rows


def finalize(rows: list[Row], t0: datetime) -> list[dict[str, Any]]:
    """Stable order (time, then lane), ``seq`` from 1, ``t`` seconds since ``t0``."""
    ordered = sorted(rows, key=lambda r: (r.at, LANE_ORDER.get(r.lane, 9)))
    out: list[dict[str, Any]] = []
    for seq, row in enumerate(ordered, 1):
        body: dict[str, Any] = {
            "seq": seq,
            "at": iso(row.at),
            "t": round((row.at - t0).total_seconds(), 3),
            "lane": row.lane,
            "kind": row.kind,
            "title": row.title,
        }
        body.update({k: v for k, v in row.extra.items() if v is not None and v != ""})
        out.append(body)
    return out


def event_time(event: HistoryEvent) -> datetime | None:
    return ts(event.event_time)
