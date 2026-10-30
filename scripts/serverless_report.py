"""Which Worker lifetimes ran a Support conversation, and on which version (v0.7).

    uv run python scripts/serverless_report.py support-2077-sl [--min-lifetimes 2] [--build-id X]

Reads the Workflow's history in namespace ``agents`` and groups its Workflow Task
completions and Activities by Worker identity. AgentCore-shaped Workers name themselves
``<pid>@<host>/agentcore-lifetime-<n>`` (``support_agent.agentcore_app``), so each group
is one Worker lifetime (one AgentCore session in the cloud). Every Workflow Task records
the Worker Deployment Version and the versioning behaviour it ran with, so the report
shows that all lifetimes ran the same Build ID, PINNED. With ``--min-lifetimes`` /
``--build-id`` it exits non-zero when that is not so (the verify script's assertion).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime

from temporalio.api.enums.v1 import EventType, VersioningBehavior

from support_agent import config


@dataclass
class Lifetime:
    first: datetime
    last: datetime | None = None
    wft: int = 0
    act: int = 0


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else "")
    parser.add_argument("workflow_id")
    parser.add_argument("--min-lifetimes", type=int, default=0)
    parser.add_argument("--build-id", default="")
    args = parser.parse_args()

    client = await config.connect()
    handle = client.get_workflow_handle(args.workflow_id)
    history = await handle.fetch_history()
    desc = await handle.describe()
    vinfo = desc.raw_description.workflow_execution_info.versioning_info

    lifetimes: dict[str, Lifetime] = {}
    versions: set[str] = set()
    behaviors: set[str] = set()
    for e in history.events:
        when = e.event_time.ToDatetime()
        if e.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED:
            a = e.workflow_task_completed_event_attributes
            who = lifetimes.setdefault(a.identity, Lifetime(first=when))
            who.wft += 1
            who.last = when
            v = a.deployment_version
            versions.add(f"{v.deployment_name}:{v.build_id}" if v.build_id else "unversioned")
            behaviors.add(VersioningBehavior.Name(a.versioning_behavior))
        elif e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_STARTED:
            ident = e.activity_task_started_event_attributes.identity
            lifetimes.setdefault(ident, Lifetime(first=when)).act += 1

    status = desc.status.name if desc.status else "?"
    print(f"{args.workflow_id}: {status}, {len(lifetimes)} Worker lifetimes")
    for ident, w in lifetimes.items():
        span = w.first.strftime("%H:%M:%S")
        if w.last:
            span += "-" + w.last.strftime("%H:%M:%S")
        print(f"  {ident:55s} {span}  workflow tasks {w.wft:>2}, activities {w.act:>2}")
    print(f"  versions on its Workflow Tasks: {sorted(versions)}; behaviour: {sorted(behaviors)}")
    dv = vinfo.deployment_version
    print(
        f"  describe: versioningInfo.behavior={VersioningBehavior.Name(vinfo.behavior)} "
        f"deploymentVersion={dv.deployment_name}:{dv.build_id}"
    )

    problems = []
    if len(lifetimes) < args.min_lifetimes:
        problems.append(f"expected >= {args.min_lifetimes} Worker lifetimes, saw {len(lifetimes)}")
    if args.build_id and versions != {f"{config.deployment_name()}:{args.build_id}"}:
        problems.append(f"expected only {config.deployment_name()}:{args.build_id}, saw {versions}")
    if args.build_id and behaviors != {"VERSIONING_BEHAVIOR_PINNED"}:
        problems.append(f"expected PINNED on every Workflow Task, saw {behaviors}")
    for p in problems:
        print(f"  FAIL {p}")
    if not problems and (args.min_lifetimes or args.build_id):
        print("  ok   one Build ID, PINNED, across every lifetime")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
