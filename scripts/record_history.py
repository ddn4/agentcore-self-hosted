"""Record a Workflow's event history as a replay-test fixture.

    uv run python scripts/record_history.py support-1042 [--out DIR] [--name NAME] [--namespace NS]

Fetches the history from the running server through the same envconfig
connection the Support Worker uses, blanks client/Worker ``identity`` fields
and host names in sticky queue names (they embed the local user and
hostname), and writes
``services/support-agent/tests/histories/<workflow-id>.json``, which
``tests/test_replay.py`` replays. Equivalent to
``temporal workflow show -w <id> -o json`` plus the identity scrub.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from support_agent import config

DEFAULT_OUT = Path("services/support-agent/tests/histories")


# Sticky task queues are named "<pid>@<hostname>-<32 hex>".
_STICKY = re.compile(r"\b\d+@[\w.-]+?-([0-9a-f]{32})\b")


def scrub(node: Any) -> Any:
    """Blank identities and drop host names from sticky queue names."""
    if isinstance(node, dict):
        return {k: ("" if k == "identity" else scrub(v)) for k, v in node.items()}
    if isinstance(node, list):
        return [scrub(v) for v in node]
    if isinstance(node, str):
        return _STICKY.sub(r"sticky-\1", node)
    return node


async def main() -> int:
    parser = argparse.ArgumentParser(description="Record a Workflow history as a replay fixture")
    parser.add_argument("workflow_id")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--name", help="fixture name (default: the Workflow ID)")
    parser.add_argument("--namespace", help="namespace (default: the Support config's, agents)")
    args = parser.parse_args()

    if args.namespace:
        os.environ["TEMPORAL_NAMESPACE"] = args.namespace
    client = await config.connect(plugins=[config.build_strands_plugin()])
    handle = client.get_workflow_handle(args.workflow_id)
    desc = await handle.describe()
    if desc.status is None or desc.status.name != "COMPLETED":
        print(f"{args.workflow_id}: status {desc.status}; record only completed runs")
        return 1
    history = await handle.fetch_history()
    data = scrub(json.loads(history.to_json()))
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"{args.name or args.workflow_id}.json"
    path.write_text(json.dumps(data, indent=2) + "\n")
    print(f"recorded {len(history.events)} events of {args.workflow_id} -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
