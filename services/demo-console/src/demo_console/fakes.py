"""Fakes for the console's unit tests (DESIGN.md 6.1): no Temporal, no AWS, no processes.

A test seam, like ``payments_mock.server.serve_in_thread``: the service never uses it.

- ``FakeGateway``: canned histories (recorded from a scripted local run, hostname scrubbed),
  describe responses built from them, ``get_task`` answers, and a log of every call.
- ``FakeRunner``: records argv/env, scripted exit codes and output; fake process groups.
- ``FakeHttp``: ``/ping``, ``/invocations``, payments and TCP ports.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from temporalio.api.common.v1 import WorkflowExecution
from temporalio.api.enums.v1 import EventType, PendingNexusOperationState, WorkflowExecutionStatus
from temporalio.api.workflow.v1 import PendingNexusOperationInfo
from temporalio.api.workflowservice.v1 import DescribeWorkflowExecutionResponse
from temporalio.client import WorkflowHistory

from demo_console.clients import Execution, NotFound, SupportQueryForbidden, TemporalUnreachable
from demo_console.runner import CommandResult
from demo_console.settings import AGENTS_NS, Settings

E = EventType


def load_history(path: Path, workflow_id: str = "wf") -> WorkflowHistory:
    return WorkflowHistory.from_json(workflow_id, path.read_text())


def truncate(hist: WorkflowHistory, keep: int) -> WorkflowHistory:
    """The first ``keep`` events (a run "as it was" mid-flight)."""
    return WorkflowHistory(hist.workflow_id, list(hist.events)[:keep])


def until(hist: WorkflowHistory, event_type: int, nth: int = 1) -> WorkflowHistory:
    """Events up to and including the ``nth`` event of ``event_type``."""
    seen = 0
    for i, e in enumerate(hist.events):
        if e.event_type == event_type:
            seen += 1
            if seen == nth:
                return truncate(hist, i + 1)
    raise AssertionError(f"no event {event_type} #{nth}")


_S = WorkflowExecutionStatus
_CLOSE = {
    E.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED: _S.WORKFLOW_EXECUTION_STATUS_COMPLETED,
    E.EVENT_TYPE_WORKFLOW_EXECUTION_FAILED: _S.WORKFLOW_EXECUTION_STATUS_FAILED,
    E.EVENT_TYPE_WORKFLOW_EXECUTION_TERMINATED: _S.WORKFLOW_EXECUTION_STATUS_TERMINATED,
}


def describe_from(
    hist: WorkflowHistory, *, run_id: str = "run-1", pending: list[dict[str, Any]] | None = None
) -> DescribeWorkflowExecutionResponse:
    events = list(hist.events)
    desc = DescribeWorkflowExecutionResponse()
    info = desc.workflow_execution_info
    info.execution.CopyFrom(WorkflowExecution(workflow_id=hist.workflow_id, run_id=run_id))
    info.start_time.CopyFrom(events[0].event_time)
    last = events[-1]
    info.status = _CLOSE.get(
        last.event_type, WorkflowExecutionStatus.WORKFLOW_EXECUTION_STATUS_RUNNING
    )
    if last.event_type in _CLOSE:
        info.close_time.CopyFrom(last.event_time)
    for e in events:
        if e.event_type == E.EVENT_TYPE_WORKFLOW_TASK_COMPLETED:
            dv = e.workflow_task_completed_event_attributes.deployment_version
            if dv.build_id:
                info.versioning_info.deployment_version.CopyFrom(dv)
                info.versioning_info.behavior = (
                    e.workflow_task_completed_event_attributes.versioning_behavior
                )
    for op in pending or []:
        p = PendingNexusOperationInfo(
            endpoint="finance-agent",
            service="FinanceAgentA2A",
            operation=op["operation"],
            attempt=op.get("attempt", 1),
            state=op.get(
                "state", PendingNexusOperationState.PENDING_NEXUS_OPERATION_STATE_BACKING_OFF
            ),
        )
        p.schedule_to_close_timeout.FromTimedelta(timedelta(hours=25))
        p.scheduled_time.CopyFrom(events[-1].event_time)
        p.last_attempt_complete_time.CopyFrom(events[-1].event_time)
        if op.get("failure"):
            p.last_attempt_failure.message = op["failure"]
        desc.pending_nexus_operations.append(p)
    return desc


@dataclass
class FakeGateway:
    """Implements ``clients.Gateway`` from canned data; records every call."""

    histories: dict[tuple[str, str], WorkflowHistory] = field(default_factory=dict)
    describes: dict[tuple[str, str], DescribeWorkflowExecutionResponse] = field(
        default_factory=dict
    )
    queries: dict[tuple[str, str, str], Any] = field(default_factory=dict)
    pollers_by_queue: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    endpoint: dict[str, Any] | None = None
    deployment_info: dict[str, Any] | None = None
    version_info: dict[str, Any] | None = None
    executions: dict[str, list[Execution]] = field(default_factory=dict)
    up: bool = True
    calls: list[tuple[str, str, str]] = field(default_factory=list)
    signals: list[tuple[str, str, str, Any]] = field(default_factory=list)
    starts: list[dict[str, Any]] = field(default_factory=list)
    terminated: list[tuple[str, str, str]] = field(default_factory=list)

    def add_run(
        self,
        wf: str,
        support: WorkflowHistory,
        finance: WorkflowHistory | None = None,
        *,
        pending: list[dict[str, Any]] | None = None,
        task: Any = None,
    ) -> None:
        support = WorkflowHistory(wf, list(support.events))
        self.histories[(AGENTS_NS, wf)] = support
        self.describes[(AGENTS_NS, wf)] = describe_from(support, pending=pending)
        if finance is not None:
            fid = f"a2a-task-{wf}"
            finance = WorkflowHistory(fid, list(finance.events))
            self.histories[("agent-services", fid)] = finance
            self.describes[("agent-services", fid)] = describe_from(finance, run_id="run-f")
            if task is not None:
                self.queries[("agent-services", fid, "get_task")] = task

    # --- Gateway -------------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self.up

    @property
    def last_error(self) -> str | None:
        return None if self.up else "connection refused"

    async def ensure(self) -> bool:
        return self.up

    async def close(self) -> None:
        self.calls.append(("close", "", ""))

    async def system_info(self) -> dict[str, Any]:
        self.calls.append(("system_info", "", ""))
        self._check()
        return {"server_version": "1.32.0"}

    async def fetch_history(
        self, namespace: str, workflow_id: str, run_id: str | None = None
    ) -> WorkflowHistory:
        self.calls.append(("fetch_history", namespace, workflow_id))
        self._check()
        key = (
            namespace,
            workflow_id if run_id in (None, "run-1", "run-f") else f"{workflow_id}@{run_id}",
        )
        if key not in self.histories:
            raise NotFound(workflow_id)
        return self.histories[key]

    async def describe(
        self, namespace: str, workflow_id: str, run_id: str | None = None
    ) -> DescribeWorkflowExecutionResponse:
        self.calls.append(("describe", namespace, workflow_id))
        self._check()
        if (namespace, workflow_id) not in self.describes:
            raise NotFound(workflow_id)
        return self.describes[(namespace, workflow_id)]

    async def list_workflows(self, namespace: str, query: str, limit: int = 25) -> list[Execution]:
        self.calls.append(("list_workflows", namespace, query))
        self._check()
        return list(self.executions.get(f"{namespace}|{query}", []))[:limit]

    async def query(self, namespace: str, workflow_id: str, name: str, within: float = 5.0) -> Any:
        self.calls.append(("query", namespace, workflow_id))
        self._check()
        if namespace == AGENTS_NS:
            raise SupportQueryForbidden("ledger 72")
        value = self.queries.get((namespace, workflow_id, name))
        if value is None:
            raise TimeoutError("query timed out (no poller)")
        if isinstance(value, BaseException):
            raise value
        return value() if callable(value) else value

    async def signal(self, namespace: str, workflow_id: str, name: str, arg: Any) -> None:
        self.calls.append(("signal", namespace, workflow_id))
        self._check()
        self.signals.append((namespace, workflow_id, name, arg))

    async def start_workflow(
        self,
        namespace: str,
        workflow_type: str,
        args: list[Any],
        *,
        workflow_id: str,
        task_queue: str,
        execution_timeout: timedelta | None = None,
    ) -> tuple[str, bool]:
        self.calls.append(("start_workflow", namespace, workflow_id))
        self._check()
        self.starts.append(
            {
                "namespace": namespace,
                "type": workflow_type,
                "args": args,
                "workflow_id": workflow_id,
                "task_queue": task_queue,
            }
        )
        return "run-new", (namespace, workflow_id) in self.describes

    async def terminate(self, namespace: str, workflow_id: str, reason: str) -> None:
        self.calls.append(("terminate", namespace, workflow_id))
        self._check()
        self.terminated.append((namespace, workflow_id, reason))

    async def pollers(self, namespace: str, task_queue: str) -> list[dict[str, Any]]:
        self.calls.append(("pollers", namespace, task_queue))
        self._check()
        return list(self.pollers_by_queue.get(f"{namespace}/{task_queue}", []))

    async def nexus_endpoint(self, name: str) -> dict[str, Any] | None:
        self.calls.append(("nexus_endpoint", "agents", name))
        self._check()
        return self.endpoint

    async def deployment(self, name: str) -> dict[str, Any] | None:
        self.calls.append(("deployment", "agents", name))
        self._check()
        return self.deployment_info

    async def deployment_version(self, name: str, build_id: str) -> dict[str, Any] | None:
        self.calls.append(("deployment_version", "agents", name))
        self._check()
        return self.version_info

    def _check(self) -> None:
        if not self.up:
            raise TemporalUnreachable("connection refused")

    def support_queries(self) -> list[tuple[str, str, str]]:
        return [c for c in self.calls if c[0] == "query" and c[1] == AGENTS_NS]


@dataclass
class FakeSpawned:
    pid: int
    done: asyncio.Event = field(default_factory=asyncio.Event)
    code: int = 0

    async def wait(self) -> int:
        await self.done.wait()
        return self.code


class FakeRunner:
    """``CommandRunner`` that runs nothing."""

    def __init__(self) -> None:
        self.ran: list[tuple[list[str], dict[str, str]]] = []
        self.spawned: list[tuple[list[str], dict[str, str], Path]] = []
        self.signals: list[tuple[int, int]] = []
        self.alive_groups: set[int] = set()
        self.alive_pids: set[int] = set()
        self.results: dict[str, CommandResult] = {}
        self.children: dict[int, FakeSpawned] = {}
        self.ignore_term: set[int] = set()
        self._next = 4000

    def result_for(self, prefix: str, code: int, output: str = "") -> None:
        self.results[prefix] = CommandResult([prefix], code, output)

    async def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: Path,
        within: float = 60.0,
    ) -> CommandResult:
        self.ran.append((list(argv), dict(env)))
        joined = " ".join(argv)
        for prefix, res in self.results.items():
            if joined.startswith(prefix):
                return CommandResult(list(argv), res.returncode, res.output)
        return CommandResult(list(argv), 0, "")

    async def spawn(
        self, argv: Sequence[str], *, env: Mapping[str, str], cwd: Path, log_path: Path
    ) -> FakeSpawned:
        self._next += 1
        pid = self._next
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a") as f:
            f.write(f"started {' '.join(argv)}\n")
        self.spawned.append((list(argv), dict(env), log_path))
        self.alive_groups.add(pid)
        child = FakeSpawned(pid)
        self.children[pid] = child
        return child

    def exit(self, pid: int, code: int = 0) -> None:
        self.alive_groups.discard(pid)
        child = self.children.get(pid)
        if child is not None:
            child.code = code
            child.done.set()

    def signal_group(self, pgid: int, sig: int) -> bool:
        self.signals.append((pgid, sig))
        if sig == 9 or pgid not in self.ignore_term:
            self.exit(pgid, -sig)
        return True

    def group_alive(self, pgid: int) -> bool:
        return pgid in self.alive_groups

    def pid_alive(self, pid: int) -> bool:
        return pid in self.alive_pids or pid in self.alive_groups


class FakeHttp:
    def __init__(self) -> None:
        self.ports: set[int] = set()
        self.get_answers: dict[str, tuple[int, Any] | None] = {}
        self.post_answers: dict[str, tuple[int, Any] | None] = {}
        self.posts: list[tuple[str, Any, dict[str, str]]] = []

    async def get(self, url: str, within: float = 2.0) -> tuple[int, Any] | None:
        return self.get_answers.get(url)

    async def post(
        self, url: str, body: Any, headers: dict[str, str], within: float = 10.0
    ) -> tuple[int, Any] | None:
        self.posts.append((url, body, headers))
        return self.post_answers.get(url)

    async def tcp_open(self, host: str, port: int, within: float = 1.0) -> bool:
        return port in self.ports


def make_settings(tmp_path: Path, profile: str = "local", **env: str) -> Settings:
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".temporal").mkdir(parents=True, exist_ok=True)
    base = {"PATH": "/usr/bin:/bin", "TEMPORAL_TLS": "false", "TEMPORAL_ADDRESS": "127.0.0.1:7233"}
    base.update(env)
    return Settings(root=tmp_path, profile=profile, base_env=base)


def task_value(
    state: str, text: str = "", artifact: dict[str, Any] | None = None
) -> dict[str, Any]:
    """A ``get_task`` answer (A2ATask as JSON)."""
    task: dict[str, Any] = {
        "id": "a2a-task-x",
        "context_id": "x",
        "status": {
            "state": state,
            "message": {"role": "agent", "parts": [{"kind": "text", "text": text}]},
            "timestamp": "2026-11-02T16:09:20.295000+00:00",
        },
        "artifacts": [],
    }
    if artifact:
        task["artifacts"] = [
            {
                "artifact_id": "a",
                "name": "refund_confirmation",
                "parts": [{"kind": "data", "data": artifact}],
            }
        ]
    return task


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
