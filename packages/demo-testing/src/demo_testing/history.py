"""Event-history helpers for assertions in tests and verify scripts.

They read a ``temporalio.client.WorkflowHistory`` (from ``handle.fetch_history()``
or ``WorkflowHistory.from_json``) and answer the questions the story asks:
"which Activities ran, in what order, how many attempts did each take".
"""

from __future__ import annotations

from dataclasses import dataclass

from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowHistory


@dataclass(frozen=True)
class ActivityRun:
    """One scheduled Activity and how it ended."""

    activity_id: str
    activity_type: str
    attempts: int
    completed: bool


def activity_runs(history: WorkflowHistory) -> list[ActivityRun]:
    """Scheduled Activities in schedule order, with their final attempt number."""
    scheduled: dict[int, tuple[str, str]] = {}
    attempts: dict[int, int] = {}
    completed: set[int] = set()
    for event in history.events:
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            attrs = event.activity_task_scheduled_event_attributes
            scheduled[event.event_id] = (attrs.activity_id, attrs.activity_type.name)
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
        )
        for event_id, (activity_id, activity_type) in scheduled.items()
    ]


def activity_types(history: WorkflowHistory) -> list[str]:
    """Activity type names in the order they were scheduled."""
    return [run.activity_type for run in activity_runs(history)]
