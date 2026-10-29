"""Show both sides of the A2A hop: caller (namespace agents) and Finance task (agent-services).

    uv run python scripts/a2a_timeline.py smoke-1042

Caller: each Nexus operation (endpoint, service.operation, summary, outcome and
the Workflow it links to in the other namespace). Handler: the A2A task
Workflow ``a2a-task-<caller id>``, its Activities (who was thinking, which tools
ran, the refund) and its ``get_task`` state. Uses the envconfig connection.
"""

from __future__ import annotations

import asyncio
import sys

from temporalio.client import Client

from demo_testing.history import activity_runs, nexus_operations
from finance_a2a_api import A2ATask, first_data, status_text
from support_agent import config

HANDLER_NAMESPACE = "agent-services"


async def main(workflow_id: str) -> int:
    caller = await config.connect()
    handler = await Client.connect(**{**config.connect_config(), "namespace": HANDLER_NAMESPACE})
    history = await caller.get_workflow_handle(workflow_id).fetch_history()
    print(f"caller  {caller.namespace}/{workflow_id}: {len(history.events)} events")
    for n, op in enumerate(nexus_operations(history), 1):
        kind = "async" if op.started else "sync "
        print(f"  {n}. {kind} {op.endpoint} -> {op.service}.{op.operation:<15} {op.outcome}")
        print(f"       summary: {op.summary}")
        for link in op.links:
            print(f"       linked:  {link}")
    task_id = f"a2a-task-{workflow_id}"
    handle = handler.get_workflow_handle(task_id)
    desc = await handle.describe()
    print(f"handler {HANDLER_NAMESPACE}/{task_id}: {desc.status.name if desc.status else '?'}")
    print(f"       summary: {await desc.static_summary()}")
    for n, run in enumerate(activity_runs(await handle.fetch_history()), 1):
        label = f"{run.activity_type} [{run.summary}]" if run.summary else run.activity_type
        print(
            f"  {n}. {label:<55} attempts={run.attempts} {'completed' if run.completed else 'open'}"
        )
    task = await handle.query("get_task", result_type=A2ATask)
    print(f"get_task: state={task.status.state}: {status_text(task)}")
    for artifact in task.artifacts:
        print(f"  artifact {artifact.name}: {first_data(artifact.parts)}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(asyncio.run(main(sys.argv[1])))
