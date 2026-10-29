"""Event-history helpers for assertions in tests and verify scripts.

They read a ``temporalio.client.WorkflowHistory`` (from ``handle.fetch_history()``
or ``WorkflowHistory.from_json``) and answer the questions the story asks:
"which Activities ran, in what order, how many attempts did each take" (v0.2)
and "which agent was thinking" (v0.3: the ``summary`` each ``TemporalAgent``
puts on its ``invoke_model`` Activities, stored as the scheduled event's
``user_metadata.summary``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from temporalio.api.common.v1 import Payload
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowHistory


@dataclass(frozen=True)
class ActivityRun:
    """One scheduled Activity and how it ended."""

    activity_id: str
    activity_type: str
    attempts: int
    completed: bool
    #: The Activity's summary (Web UI "Summary"); "" when none was set.
    summary: str = ""


def _summary(payload: Payload) -> str:
    if not payload.data:
        return ""
    try:
        value = json.loads(payload.data)
    except ValueError:
        return payload.data.decode("utf-8", "replace")
    return value if isinstance(value, str) else str(value)


def activity_runs(history: WorkflowHistory) -> list[ActivityRun]:
    """Scheduled Activities in schedule order, with their final attempt number."""
    scheduled: dict[int, tuple[str, str, str]] = {}
    attempts: dict[int, int] = {}
    completed: set[int] = set()
    for event in history.events:
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            attrs = event.activity_task_scheduled_event_attributes
            scheduled[event.event_id] = (
                attrs.activity_id,
                attrs.activity_type.name,
                _summary(event.user_metadata.summary),
            )
        elif event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_STARTED:
            attrs = event.activity_task_started_event_attributes
            attempts[attrs.scheduled_event_id] = attrs.attempt
        elif event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED:
            completed.add(event.activity_task_completed_event_attributes.scheduled_event_id)
    return [
        ActivityRun(
            activity_id=activity_id,
            activity_type=activity_type,
            attempts=attempts.get(event_id, 0),
            completed=event_id in completed,
            summary=summary,
        )
        for event_id, (activity_id, activity_type, summary) in scheduled.items()
    ]


def activity_types(history: WorkflowHistory) -> list[str]:
    """Activity type names in the order they were scheduled."""
    return [run.activity_type for run in activity_runs(history)]


def attributed_activities(history: WorkflowHistory) -> list[str]:
    """``"<type>"`` or ``"<type> [<summary>]"`` per scheduled Activity, in order.

    For v0.3: ``invoke_model [Order Lookup agent]`` vs ``invoke_model [Support Supervisor]``.
    """
    return [
        f"{run.activity_type} [{run.summary}]" if run.summary else run.activity_type
        for run in activity_runs(history)
    ]


def count_events(history: WorkflowHistory, event_type: EventType.ValueType) -> int:
    """How many events of ``event_type`` the history holds (e.g. timers)."""
    return sum(1 for event in history.events if event.event_type == event_type)


# --- v0.4: Nexus operations (the A2A hop) ---------------------------------------


@dataclass(frozen=True)
class NexusOperationRun:
    """One Nexus operation a caller Workflow scheduled, and what became of it."""

    endpoint: str
    service: str
    operation: str
    summary: str
    #: "completed", "failed", "canceled", "timed out" or "pending".
    outcome: str
    #: Async operations only: the handler Workflow the operation started.
    started: bool
    #: ``namespace/workflow_id`` of each Workflow the operation links to.
    links: tuple[str, ...]


_NEXUS_END = {
    EventType.EVENT_TYPE_NEXUS_OPERATION_COMPLETED: (
        "completed",
        "nexus_operation_completed_event_attributes",
    ),
    EventType.EVENT_TYPE_NEXUS_OPERATION_FAILED: (
        "failed",
        "nexus_operation_failed_event_attributes",
    ),
    EventType.EVENT_TYPE_NEXUS_OPERATION_CANCELED: (
        "canceled",
        "nexus_operation_canceled_event_attributes",
    ),
    EventType.EVENT_TYPE_NEXUS_OPERATION_TIMED_OUT: (
        "timed out",
        "nexus_operation_timed_out_event_attributes",
    ),
}


def nexus_operations(history: WorkflowHistory) -> list[NexusOperationRun]:
    """Nexus operations in schedule order (caller side), with summary, outcome and links."""
    scheduled: dict[int, tuple[str, str, str, str]] = {}
    started: dict[int, tuple[str, ...]] = {}
    outcome: dict[int, str] = {}
    for event in history.events:
        if event.event_type == EventType.EVENT_TYPE_NEXUS_OPERATION_SCHEDULED:
            attrs = event.nexus_operation_scheduled_event_attributes
            scheduled[event.event_id] = (
                attrs.endpoint,
                attrs.service,
                attrs.operation,
                _summary(event.user_metadata.summary),
            )
        elif event.event_type == EventType.EVENT_TYPE_NEXUS_OPERATION_STARTED:
            sid = event.nexus_operation_started_event_attributes.scheduled_event_id
            started[sid] = tuple(
                f"{link.workflow_event.namespace}/{link.workflow_event.workflow_id}"
                for link in event.links
                if link.HasField("workflow_event")
            )
        elif event.event_type in _NEXUS_END:
            label, field_name = _NEXUS_END[event.event_type]
            outcome[getattr(event, field_name).scheduled_event_id] = label
    return [
        NexusOperationRun(
            endpoint=endpoint,
            service=service,
            operation=operation,
            summary=summary,
            outcome=outcome.get(event_id, "pending"),
            started=event_id in started,
            links=started.get(event_id, ()),
        )
        for event_id, (endpoint, service, operation, summary) in scheduled.items()
    ]
