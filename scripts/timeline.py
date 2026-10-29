"""One timeline for a Support conversation, both sides of the A2A hop (v0.5).

    uv run python scripts/timeline.py support-1042        (make timeline WF=support-1042)

Caller (namespace ``agents``): every Activity and Nexus operation in the order
the Supervisor Workflow scheduled them: who was thinking (``invoke_model
[<agent>]``), which tools ran, and where the A2A hop happened. The
``send_message`` line expands into the Finance task in namespace
``agent-services`` (its own agent turns, tools and refund). Then the A2A
conversation side by side (what Support sent, what Finance answered) and the
reply the customer gets. Honours TEMPORAL_PROFILE / TEMPORAL_CONFIG_FILE.
"""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap

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


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(asyncio.run(main(sys.argv[1])))
