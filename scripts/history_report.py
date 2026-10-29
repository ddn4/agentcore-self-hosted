"""Print a Workflow's Activities in schedule order, with attempts and which agent ran them.

    uv run python scripts/history_report.py support-1042

v0.2: which Activities ran, and which one was retried after the Worker died.
v0.3: the ``summary`` column says which agent each ``invoke_model`` belongs to
(Support Supervisor, Order Lookup agent, Return Policy agent); sub-agent tool
Activities are indented under the turn that called them. Uses the same envconfig
connection as the Worker.
"""

from __future__ import annotations

import asyncio
import sys

from demo_testing.history import activity_runs
from support_agent import config

SUPERVISOR = "Support Supervisor"


async def main(workflow_id: str) -> int:
    client = await config.connect(plugins=[config.build_strands_plugin()])
    history = await client.get_workflow_handle(workflow_id).fetch_history()
    print(f"{workflow_id}: {len(history.events)} events")
    in_sub_agent = False
    for n, run in enumerate(activity_runs(history), 1):
        status = "completed" if run.completed else "not completed"
        if run.activity_type == "invoke_model":
            in_sub_agent = bool(run.summary) and run.summary != SUPERVISOR
        indent = "   " if in_sub_agent or (run.summary and run.summary != SUPERVISOR) else ""
        who = f"[{run.summary}]" if run.summary else ""
        line = (
            f"  {n:>2}. {indent}{run.activity_type:<18} {who:<22} attempts={run.attempts} {status}"
        )
        print(line.rstrip())
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "support-1042")))
