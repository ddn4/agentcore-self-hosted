"""How long a Support conversation waited for Finance's human approver, from both histories (v0.6).

    uv run python scripts/approval_report.py support-2077        (make approval-report WF=...)

Support side (namespace ``agents``): when ``send_message`` was scheduled, started
and completed, how long the operation stayed open, and how many Workflow Tasks
Support ran while it was open (0 = the Supervisor was parked inside its
``ask_finance_agent`` tool, costing nothing; any Worker could have been off).
The Worker identities before and after the wait show whether a different Support
Worker finished the conversation.

Finance side (namespace ``agent-services``): the approval wait timer, the
``approve``/``reject`` signal (who, when) and the final A2A state.

Evidence for plan v0.6 ("an approval after >= 30 min of real time works", Q19b).
Output only has Workflow IDs, timestamps and approver names: paste-safe after
``scripts/redact.sh``. Honours TEMPORAL_PROFILE / TEMPORAL_CONFIG_FILE.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime

from temporalio.api.enums.v1 import EventType
from temporalio.api.history.v1 import HistoryEvent
from temporalio.client import Client

from support_agent import config

E = EventType


def _at(event: HistoryEvent) -> datetime:
    return event.event_time.ToDatetime()


def _fmt(seconds: float) -> str:
    minutes, sec = divmod(round(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m{sec:02d}s"


async def main() -> None:
    parser = argparse.ArgumentParser(description="Approval wait across both namespaces")
    parser.add_argument("workflow_id", help="the Support conversation (e.g. support-2077)")
    args = parser.parse_args()

    support = await Client.connect(**config.connect_config())
    history = await support.get_workflow_handle(args.workflow_id).fetch_history()
    events = list(history.events)
    sched = next(
        e
        for e in events
        if e.event_type == E.EVENT_TYPE_NEXUS_OPERATION_SCHEDULED
        and e.nexus_operation_scheduled_event_attributes.operation == "send_message"
    )
    sched_id = sched.event_id
    started = next(
        e
        for e in events
        if e.event_type == E.EVENT_TYPE_NEXUS_OPERATION_STARTED
        and e.nexus_operation_started_event_attributes.scheduled_event_id == sched_id
    )
    ends = {
        E.EVENT_TYPE_NEXUS_OPERATION_COMPLETED: "completed",
        E.EVENT_TYPE_NEXUS_OPERATION_FAILED: "failed",
        E.EVENT_TYPE_NEXUS_OPERATION_TIMED_OUT: "timed out",
        E.EVENT_TYPE_NEXUS_OPERATION_CANCELED: "canceled",
    }
    end = next(e for e in events if e.event_type in ends and e.event_id > started.event_id)
    budget = sched.nexus_operation_scheduled_event_attributes.schedule_to_close_timeout
    tasks_during = [
        e
        for e in events
        if e.event_type == E.EVENT_TYPE_WORKFLOW_TASK_STARTED
        and started.event_id < e.event_id < end.event_id
    ]
    identities = [
        e.workflow_task_started_event_attributes.identity
        for e in events
        if e.event_type == E.EVENT_TYPE_WORKFLOW_TASK_STARTED
    ]
    before = [
        e.workflow_task_started_event_attributes.identity
        for e in events
        if e.event_type == E.EVENT_TYPE_WORKFLOW_TASK_STARTED and e.event_id < started.event_id
    ]
    after = [
        e.workflow_task_started_event_attributes.identity
        for e in events
        if e.event_type == E.EVENT_TYPE_WORKFLOW_TASK_STARTED and e.event_id > end.event_id
    ]
    waited = (_at(end) - _at(started)).total_seconds()
    print(f"Support {args.workflow_id} (agents):")
    print(f"  send_message scheduled   {_at(sched).isoformat()}  budget {budget.ToSeconds()}s")
    print(f"  send_message started     {_at(started).isoformat()}  (Finance task running)")
    print(f"  send_message {ends[end.event_type]:<11} {_at(end).isoformat()}")
    print(f"  operation open for       {_fmt(waited)}")
    print(f"  Support Workflow Tasks while open: {len(tasks_during)}")
    print(
        f"  Support Workers: {len(set(identities))} distinct identities; "
        f"same before/after the wait: {bool(before and after and before[-1] == after[0])}"
    )

    finance = await Client.connect(**{**config.connect_config(), "namespace": "agent-services"})
    task_id = f"a2a-task-{args.workflow_id}"
    fh = await finance.get_workflow_handle(task_id).fetch_history()
    print(f"Finance {task_id} (agent-services):")
    for e in fh.events:
        if e.event_type == E.EVENT_TYPE_TIMER_STARTED:
            attrs = e.timer_started_event_attributes
            label = json.loads(e.user_metadata.summary.data) if e.user_metadata.summary.data else ""
            print(
                f"  timer started            {_at(e).isoformat()}  "
                f"{attrs.start_to_fire_timeout.ToSeconds()}s  {label}"
            )
        elif e.event_type in (E.EVENT_TYPE_TIMER_FIRED, E.EVENT_TYPE_TIMER_CANCELED):
            what = "fired" if e.event_type == E.EVENT_TYPE_TIMER_FIRED else "canceled"
            print(f"  timer {what:<18} {_at(e).isoformat()}")
        elif e.event_type == E.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED:
            attrs = e.workflow_execution_signaled_event_attributes
            payload = attrs.input.payloads[0].data.decode() if attrs.input.payloads else ""
            print(f"  signal {attrs.signal_name:<17} {_at(e).isoformat()}  {payload}")
    result = await finance.get_workflow_handle(task_id).query("get_task")
    print(f"  final state: {result['status']['state']}")


if __name__ == "__main__":
    asyncio.run(main())
