"""Print a Workflow's Activities in schedule order with their attempts.

    uv run python scripts/history_report.py support-1042

The question the crash demo asks: which Activities ran, and which one was
retried after the Worker died. Uses the same envconfig connection as the Worker.
"""

from __future__ import annotations

import asyncio
import sys

from demo_testing.history import activity_runs
from support_agent import config


async def main(workflow_id: str) -> int:
    client = await config.connect(plugins=[config.build_strands_plugin()])
    history = await client.get_workflow_handle(workflow_id).fetch_history()
    print(f"{workflow_id}: {len(history.events)} events")
    for n, run in enumerate(activity_runs(history), 1):
        status = "completed" if run.completed else "not completed"
        print(f"  {n}. {run.activity_type:<18} attempts={run.attempts} {status}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "support-1042")))
