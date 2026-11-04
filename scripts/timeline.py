"""One timeline for a Support conversation, both sides of the A2A hop (v0.5).

    uv run python scripts/timeline.py support-1042        (make timeline WF=support-1042)

Caller (namespace ``agents``): every Activity and Nexus operation in the order
the Supervisor Workflow scheduled them: who was thinking (``invoke_model
[<agent>]``), which tools ran, and where the A2A hop happened. The
``send_message`` line expands into the Finance task in namespace
``agent-services`` (its own agent turns, tools and refund). Then the A2A
conversation side by side (what Support sent, what Finance answered) and the
reply the customer gets. Honours TEMPORAL_PROFILE / TEMPORAL_CONFIG_FILE.

v0.13: ``--merged`` adds one wall-clock "slide" that merges three histories: Support
(``agents``), Finance's task (``agent-services``) and the Worker Controller Instance of
Support's version (``scripts/wci-history.sh``: ``task-add-signal``, ``InvokeWorker``). It ends
with the Q15 numbers: Nexus completion -> task-add-signal -> InvokeWorker -> first Workflow
Task on the new Worker lifetime. ``make timeline WF=... MERGED=1`` adds the flag and pipes the
output through ``scripts/redact.sh``.

    uv run python scripts/timeline.py support-2077 --merged
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import textwrap
from datetime import UTC, datetime
from typing import Any

from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowHistory

from demo_testing.history import activity_runs, nexus_operations
from finance_a2a_api import A2ATask, Part, first_data, first_text, status_text
from support_agent import config

HANDLER_NAMESPACE = "agent-services"


def _summary(data: bytes) -> str:
    try:
        value = json.loads(data) if data else ""
    except ValueError:
        return data.decode("utf-8", "replace")
    return value if isinstance(value, str) else str(value)


def _wrap(text: str, indent: str) -> str:
    return textwrap.fill(text, width=100, initial_indent=indent, subsequent_indent=indent)


async def finance_side(handler: Client, task_id: str) -> None:
    handle = handler.get_workflow_handle(task_id)
    desc = await handle.describe()
    status = desc.status.name if desc.status else "?"
    print(f"        ┗ {HANDLER_NAMESPACE}/{task_id}  ({status}, not a child Workflow)")
    history = await handle.fetch_history()
    for run in activity_runs(history):
        label = f"{run.activity_type} [{run.summary}]" if run.summary else run.activity_type
        print(f"            {label}  attempts={run.attempts}")
    task = await handle.query("get_task", result_type=A2ATask)
    print(_wrap(f"get_task: state={task.status.state}: {status_text(task)}", "            "))


async def main(workflow_id: str) -> int:
    caller = await config.connect(plugins=[config.build_strands_plugin()])
    handler = await Client.connect(**{**config.connect_config(), "namespace": HANDLER_NAMESPACE})
    handle = caller.get_workflow_handle(workflow_id)
    history: WorkflowHistory = await handle.fetch_history()
    ops = {(o.operation, o.summary): o for o in nexus_operations(history)}
    runs = iter(activity_runs(history))
    a2a = await handle.query("a2a")
    print(f"{caller.namespace}/{workflow_id}: A2A mode {a2a['mode']}, {len(history.events)} events")

    n = 0
    for event in history.events:
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            run = next(runs)
            n += 1
            label = f"{run.activity_type} [{run.summary}]" if run.summary else run.activity_type
            indent = "" if run.summary in ("Support Supervisor", "") else "  "
            if run.activity_type in ("lookup_order", "get_return_policy"):
                indent = "    "
            print(f"  {n:>2}. {indent}{label}  attempts={run.attempts}")
        elif event.event_type == EventType.EVENT_TYPE_NEXUS_OPERATION_SCHEDULED:
            attrs = event.nexus_operation_scheduled_event_attributes
            summary = _summary(event.user_metadata.summary.data)
            op = ops[(attrs.operation, summary)]
            n += 1
            target = f"{attrs.endpoint}: {attrs.service}.{attrs.operation}"
            print(f"  {n:>2}. ⇢ NEXUS {target}  {op.outcome}")
            print(f"        {summary}")
            for link in op.links:
                await finance_side(handler, link.split("/", 1)[1])

    if a2a["agent_card"]:
        card = a2a["agent_card"]
        print(f"\nA2A peer: {card['name']} v{card['version']} (A2A {card['protocol_version']})")
    for exchange in a2a["exchanges"]:
        task_id = exchange["task_id"]
        if task_id:
            started = (await handler.get_workflow_handle(task_id).fetch_history()).events[0]
            payload = started.workflow_execution_started_event_attributes.input.payloads[0]
            msg = json.loads(payload.data)
            parts = [Part(**p) for p in msg["parts"]]
            print(_wrap(f"Support → Finance: {first_text(parts)}  {first_data(parts)}", "  "))
        print(_wrap(f"Finance → Support: [{exchange['state']}] {exchange['reply']}", "  "))
    if a2a["guard_reprompts"]:
        print(f"  prompt-drift guard re-prompted the Supervisor {a2a['guard_reprompts']} time(s)")
    desc = await handle.describe()
    if desc.status is not None and desc.status.name == "COMPLETED":
        reply = await handle.result()
        print("\nReply to the customer:\n" + _wrap(str(reply), "  "))
    else:
        print(f"\nno reply yet: Workflow is {desc.status.name if desc.status else '?'}")
    return 0


Row = tuple[datetime, str, str]


def _t(event: Any) -> datetime:
    return event.event_time.ToDatetime(tzinfo=UTC)


def _label(event: Any) -> str:
    attrs = event.activity_task_scheduled_event_attributes
    summary = _summary(event.user_metadata.summary.data)
    return f"{attrs.activity_type.name} [{summary}]" if summary else attrs.activity_type.name


def _support_rows(history: WorkflowHistory) -> list[Row]:
    rows: list[Row] = []
    ops: dict[int, str] = {}
    for e in history.events:
        et = e.event_type
        if et == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED:
            rows.append((_t(e), "support", "Workflow started (ticket received)"))
        elif et == EventType.EVENT_TYPE_WORKFLOW_TASK_STARTED:
            who = e.workflow_task_started_event_attributes.identity
            rows.append((_t(e), "support", f"Workflow Task on {who}"))
        elif et == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            label = _label(e)
            kind = "model turn" if label.startswith("invoke_model") else "activity"
            rows.append((_t(e), "support", f"{kind} {label}"))
        elif et == EventType.EVENT_TYPE_NEXUS_OPERATION_SCHEDULED:
            op = e.nexus_operation_scheduled_event_attributes.operation
            ops[e.event_id] = op
            rows.append((_t(e), "support", f"NEXUS {op} scheduled"))
        elif et == EventType.EVENT_TYPE_NEXUS_OPERATION_STARTED:
            sid = e.nexus_operation_started_event_attributes.scheduled_event_id
            rows.append((_t(e), "support", f"NEXUS {ops.get(sid, '?')} started (async)"))
        elif et == EventType.EVENT_TYPE_NEXUS_OPERATION_COMPLETED:
            sid = e.nexus_operation_completed_event_attributes.scheduled_event_id
            rows.append((_t(e), "support", f"NEXUS {ops.get(sid, '?')} COMPLETED"))
        elif et == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED:
            rows.append((_t(e), "support", "Workflow completed (reply sent)"))
    return rows


def _finance_rows(history: WorkflowHistory) -> list[Row]:
    rows: list[Row] = []
    for e in history.events:
        et = e.event_type
        if et == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED:
            rows.append((_t(e), "finance", "A2A task started (EKS)"))
        elif et == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            label = _label(e)
            kind = "model turn" if label.startswith("invoke_model") else "activity"
            rows.append((_t(e), "finance", f"{kind} {label}"))
        elif et == EventType.EVENT_TYPE_TIMER_STARTED:
            rows.append((_t(e), "finance", "input-required: approval timer started"))
        elif et == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED:
            name = e.workflow_execution_signaled_event_attributes.signal_name
            rows.append((_t(e), "finance", f"signal {name} (human decision)"))
        elif et == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED:
            rows.append((_t(e), "finance", "A2A task completed"))
    return rows


def _wci_rows(build: str, since: datetime, until: datetime) -> list[Row]:
    out = subprocess.run(
        ["scripts/wci-history.sh", build, "--since", since.strftime("%Y-%m-%dT%H:%M:%SZ")],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    rows: list[Row] = []
    for line in out.splitlines():
        parts = line.strip().split("\t")
        if len(parts) != 2:
            continue
        when = datetime.fromisoformat(parts[0][:26].rstrip("Z") + "+00:00")
        what = " ".join(parts[1].split())
        keep = what in ("signal task-add-signal", "activity InvokeWorker") or "failed" in what
        if keep and when <= until:
            rows.append((when, "wci", what))
    return rows


async def merged(workflow_id: str) -> int:
    caller = await config.connect(plugins=[config.build_strands_plugin()])
    handler = await Client.connect(**{**config.connect_config(), "namespace": HANDLER_NAMESPACE})
    handle = caller.get_workflow_handle(workflow_id)
    desc = await handle.describe()
    vinfo = desc.raw_description.workflow_execution_info.versioning_info
    build = vinfo.deployment_version.build_id
    rows = _support_rows(await handle.fetch_history())
    finance = handler.get_workflow_handle(f"a2a-task-{workflow_id}")
    rows += _finance_rows(await finance.fetch_history())
    rows.sort(key=lambda r: r[0])
    start, end = rows[0][0], rows[-1][0]
    if build:
        rows += _wci_rows(build, start, end)
    rows.sort(key=lambda r: r[0])
    where = f"Support version {build}" if build else "Support unversioned"
    print(f"\n== One timeline: {workflow_id} ({where}, Finance)")
    print(f"   t0 = {start:%Y-%m-%d %H:%M:%S}Z; lanes: support | finance | wci")
    for when, lane, what in rows:
        print(f"  +{(when - start).total_seconds():7.2f}s  {lane:<9} {what}")
    _q15(rows)
    return 0


def _q15(rows: list[Row]) -> None:
    done = next(
        (w for w, ln, x in rows if ln == "support" and x == "NEXUS send_message COMPLETED"), None
    )
    if done is None:
        return

    def after(lane: str, needle: str) -> datetime | None:
        return next((w for w, ln, x in rows if w >= done and ln == lane and needle in x), None)

    marks = [
        ("task-add-signal", after("wci", "task-add-signal")),
        ("InvokeWorker", after("wci", "InvokeWorker")),
        ("first Workflow Task", after("support", "Workflow Task on")),
        ("reply", after("support", "Workflow completed")),
    ]
    print("\n  Q15: after the Nexus completion (send_message COMPLETED):")
    for name, when in marks:
        delta = f"+{(when - done).total_seconds():.3f}s" if when else "not seen"
        print(f"    {name:<20} {delta}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="One timeline for a Support conversation")
    parser.add_argument("workflow_id")
    parser.add_argument("--merged", action="store_true", help="wall-clock slide incl. the WCI")
    args = parser.parse_args()
    code = asyncio.run(main(args.workflow_id))
    if args.merged:
        code = asyncio.run(merged(args.workflow_id)) or code
    sys.exit(code)
