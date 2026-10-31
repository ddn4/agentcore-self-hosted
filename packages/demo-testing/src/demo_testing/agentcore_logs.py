"""AgentCore sessions of one Workflow, read from the runtime endpoint's CloudWatch logs (v0.13).

Each AgentCore session writes its own log stream
``<date>/[runtime-logs-<session id>]<uuid>`` in the log group
``/aws/bedrock-agentcore/runtimes/<runtime id>-<endpoint>`` (seen at v0.12). The Support
Worker's lines in a stream tell the story of that session (``support_agent.agentcore_app``):

- ``AgentCore app: ...``                      the process started (cold start)
- ``Async task started: temporal-worker``     an invocation started a Worker lifetime
- ``lifetime N: connected to ...``            mTLS connect to the frontend done
- ``lifetime N: idle for 30s ...; draining``  the debounce fired
- ``worker lifetime N over; async task released``  the session may idle out now

A session "belongs" to a Workflow when one of its lines names the Workflow ID (the SDK's
workflow/activity loggers append ``workflow_id``). That is how ``scripts/demo/case-b.sh``
waits for the drain before approving, and how ``make e2e TEMPORAL_PROFILE=eks-pf`` counts
sessions per Case (A: 1, B: 2).

CLI (laptop, default SSO profile, us-east-1)::

    uv run python -m demo_testing.agentcore_logs sessions WF [--since EPOCH] [--json]
    uv run python -m demo_testing.agentcore_logs wait-drained WF [--since EPOCH] [--timeout S]
    uv run python -m demo_testing.agentcore_logs wait-idle [--since EPOCH] [--timeout S]

The endpoint is ``b_<build7>`` of ``TEMPORAL_BUILD_ID`` (or ``--build``; default
``scripts/build-id.sh``). Output has no account IDs or runtime IDs (session IDs are random
UUIDs per session and are printed shortened).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any

RUNTIME_NAME = os.environ.get("AGENTCORE_RUNTIME_NAME", "AgentcoreDemo_default_support_supervisor")
REGION = os.environ.get("AWS_REGION", "us-east-1")
_STREAM_SESSION = re.compile(r"\[runtime-logs-([0-9a-f-]+)\]")
#: Plain log lines start with a date; the JSON copies of the same lines are skipped.
_PLAIN = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")

MARKERS: tuple[tuple[str, str], ...] = (
    ("app_start", "AgentCore app:"),
    ("invoked", "Async task started: temporal-worker"),
    ("connected", ": connected to "),
    ("idle", ": idle for "),
    ("drained", ": drained"),
    ("released", "async task released"),
    ("failed", "failed"),
)


def _clients() -> tuple[Any, Any]:
    import boto3  # laptop-only path; not imported by offline tests

    session = boto3.Session(region_name=REGION)
    return session.client("bedrock-agentcore-control"), session.client("logs")


def build_id() -> str:
    env = os.environ.get("TEMPORAL_BUILD_ID")
    if env:
        return env
    root = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
    ).stdout.strip()
    done = subprocess.run([f"{root}/scripts/build-id.sh"], capture_output=True, text=True)
    if done.returncode == 0:
        return done.stdout.strip()
    # Dirty tree (build-id.sh refuses): the deployment's current version is what runs.
    out = subprocess.run(
        [
            "temporal",
            "worker",
            "deployment",
            "describe",
            "--namespace",
            "agents",
            "--name",
            os.environ.get("TEMPORAL_DEPLOYMENT_NAME", "support-supervisor"),
            "-o",
            "json",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return str(json.loads(out)["routingConfig"]["currentVersionBuildID"])


def log_group(build: str | None = None) -> str:
    control, _ = _clients()
    runtime_id = None
    for page in control.get_paginator("list_agent_runtimes").paginate():
        for rt in page.get("agentRuntimes", []):
            if rt["agentRuntimeName"] == RUNTIME_NAME:
                runtime_id = rt["agentRuntimeId"]
    if runtime_id is None:
        raise SystemExit(f"no AgentCore runtime {RUNTIME_NAME}")
    return f"/aws/bedrock-agentcore/runtimes/{runtime_id}-b_{(build or build_id())[:7]}"


@dataclass
class Session:
    """One AgentCore session (one log stream) and the Worker milestones in it."""

    session_id: str
    stream: str
    #: (epoch ms, marker, line) in time order
    events: list[tuple[int, str, str]] = field(default_factory=list)

    def first(self, marker: str) -> int | None:
        return next((ts for ts, m, _ in self.events if m == marker), None)

    def count(self, marker: str) -> int:
        return sum(1 for _, m, _ in self.events if m == marker)

    @property
    def drained(self) -> bool:
        """Every Worker lifetime started in this session has released its async task."""
        started = self.count("invoked")
        return started > 0 and self.count("released") >= started

    def summary(self) -> dict[str, Any]:
        def iso(ts: int | None) -> str | None:
            if ts is None:
                return None
            return time.strftime("%H:%M:%S", time.gmtime(ts / 1000)) + f".{ts % 1000:03d}Z"

        return {
            "session": self.session_id[:8],
            "app_start": iso(self.first("app_start")),
            "invoked": iso(self.first("invoked")),
            "connected": iso(self.first("connected")),
            "idle": iso(self.first("idle")),
            "released": iso(self.first("released")),
            "lifetimes": self.count("invoked"),
            "failed": self.count("failed"),
            "drained": self.drained,
        }


def _stream_events(logs: Any, group: str, stream: str) -> list[tuple[int, str, str]]:
    out: list[tuple[int, str, str]] = []
    kwargs: dict[str, Any] = {"logGroupName": group, "logStreamName": stream, "startFromHead": True}
    token = None
    while True:
        page = logs.get_log_events(**kwargs, **({"nextToken": token} if token else {}))
        for ev in page.get("events", []):
            line = ev["message"].rstrip("\n")
            if not _PLAIN.match(line):
                continue
            for marker, needle in MARKERS:
                if needle in line and (marker != "failed" or "worker lifetime" in line):
                    out.append((ev["timestamp"], marker, line))
                    break
        if page.get("nextForwardToken") == token:
            break
        token = page.get("nextForwardToken")
    return out


def _session(logs: Any, group: str, stream: str) -> Session:
    match = _STREAM_SESSION.search(stream)
    return Session(match.group(1) if match else stream, stream, _stream_events(logs, group, stream))


def sessions_for(workflow_id: str, since_ms: int, build: str | None = None) -> list[Session]:
    """Sessions whose log lines name ``workflow_id``, oldest first."""
    group = log_group(build)
    _, logs = _clients()
    streams: dict[str, int] = {}
    kwargs: dict[str, Any] = {
        "logGroupName": group,
        "startTime": since_ms,
        "filterPattern": json.dumps(workflow_id),
    }
    for page in logs.get_paginator("filter_log_events").paginate(**kwargs):
        for ev in page.get("events", []):
            streams.setdefault(ev["logStreamName"], ev["timestamp"])
    ordered = sorted(streams, key=lambda s: streams[s])
    return [_session(logs, group, s) for s in ordered]


def recent_sessions(since_ms: int, build: str | None = None) -> list[Session]:
    """Every session of the endpoint with a log event at or after ``since_ms``."""
    group = log_group(build)
    _, logs = _clients()
    names: list[str] = []
    resp = logs.describe_log_streams(
        logGroupName=group, orderBy="LastEventTime", descending=True, limit=50
    )
    for st in resp.get("logStreams", []):
        if st.get("lastEventTimestamp", 0) >= since_ms:
            names.append(st["logStreamName"])
    return [_session(logs, group, s) for s in reversed(names)]


def wait_drained(
    workflow_id: str, since_ms: int, timeout: float = 300, build: str | None = None
) -> list[Session]:
    """Wait until the Workflow has at least one session and all of them have drained."""
    deadline = time.monotonic() + timeout
    while True:
        found = sessions_for(workflow_id, since_ms, build)
        if found and all(s.drained for s in found):
            return found
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"{workflow_id}: sessions not drained within {timeout:g}s: "
                + json.dumps([s.summary() for s in found])
            )
        time.sleep(10)


def wait_idle(since_ms: int, timeout: float = 300, build: str | None = None) -> list[Session]:
    """Wait until every session active since ``since_ms`` has drained (nothing polls)."""
    deadline = time.monotonic() + timeout
    while True:
        found = recent_sessions(since_ms, build)
        busy = [s for s in found if s.count("invoked") and not s.drained]
        if not busy:
            return found
        if time.monotonic() > deadline:
            raise TimeoutError("sessions still polling: " + json.dumps([s.summary() for s in busy]))
        time.sleep(10)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("command", choices=["sessions", "wait-drained", "wait-idle"])
    parser.add_argument("workflow_id", nargs="?")
    parser.add_argument("--since", type=float, default=time.time() - 3600, help="epoch seconds")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--build", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    since_ms = int(args.since * 1000)
    if args.command != "wait-idle" and not args.workflow_id:
        parser.error("workflow_id is required")
    try:
        if args.command == "sessions":
            found = sessions_for(args.workflow_id, since_ms, args.build)
        elif args.command == "wait-drained":
            found = wait_drained(args.workflow_id, since_ms, args.timeout, args.build)
        else:
            found = wait_idle(since_ms, args.timeout, args.build)
    except TimeoutError as err:
        print(f"agentcore_logs: {err}", file=sys.stderr)
        return 1
    rows = [s.summary() for s in found]
    if args.json:
        print(json.dumps(rows))
    else:
        for row in rows:
            print("  " + "  ".join(f"{k}={v}" for k, v in row.items()))
        print(f"sessions: {len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
