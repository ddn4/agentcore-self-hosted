"""The crash demos' armed Kill: which model turn to cut off (runs.model_turn_to_kill)."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from temporalio.api.enums.v1 import PendingActivityState
from temporalio.api.workflow.v1 import PendingActivityInfo
from temporalio.api.workflowservice.v1 import DescribeWorkflowExecutionResponse
from temporalio.client import WorkflowHistory

from demo_console.runs import model_turn_to_kill

pytestmark = pytest.mark.unit

Load = Callable[..., WorkflowHistory]
STARTED = PendingActivityState.PENDING_ACTIVITY_STATE_STARTED
SCHEDULED = PendingActivityState.PENDING_ACTIVITY_STATE_SCHEDULED


def running(
    *ids: str, state: int = STARTED, kind: str = "invoke_model"
) -> DescribeWorkflowExecutionResponse:
    desc = DescribeWorkflowExecutionResponse()
    for i in ids:
        p = PendingActivityInfo(activity_id=i, state=state)  # type: ignore[arg-type]
        p.activity_type.name = kind
        desc.pending_activities.append(p)
    return desc


def upto(hist: WorkflowHistory, activity_id: str) -> WorkflowHistory:
    """The history as it stood when ``activity_id`` was scheduled (the run is mid-turn)."""
    events = list(hist.events)
    for n, e in enumerate(events):
        if e.activity_task_scheduled_event_attributes.activity_id == activity_id:
            return WorkflowHistory(hist.workflow_id, events[: n + 1])
    raise AssertionError(activity_id)


def test_kills_a_sub_agent_turn_not_the_supervisor_before_finance(load: Load) -> None:
    # Recorded Case A: 1 Supervisor, 2/4 Order Lookup, 5 Supervisor, 6/8 Return Policy,
    # 9 Supervisor, send_message, 10 Supervisor. get_agent_card is scheduled before turn 1.
    hist = load("case-a.support", "support-1042")
    assert model_turn_to_kill(running("1"), upto(hist, "1")) is None
    assert model_turn_to_kill(running("2"), upto(hist, "2")) == "Order Lookup agent"
    assert model_turn_to_kill(running("5"), upto(hist, "5")) is None
    assert model_turn_to_kill(running("6"), upto(hist, "6")) == "Return Policy agent"
    assert model_turn_to_kill(running("9"), upto(hist, "9")) is None
    # After send_message no sub-agent turn is left: the Supervisor's last turn will do.
    assert model_turn_to_kill(running("10"), upto(hist, "10")) == "Support Supervisor"


def test_waits_while_no_model_turn_is_in_flight(load: Load) -> None:
    hist = load("case-a.support", "support-1042")
    assert model_turn_to_kill(running(), hist) is None
    assert model_turn_to_kill(running("2", state=SCHEDULED), upto(hist, "2")) is None
    assert model_turn_to_kill(running("3", kind="lookup_order"), upto(hist, "3")) is None
