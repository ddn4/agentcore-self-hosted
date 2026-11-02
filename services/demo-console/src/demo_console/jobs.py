"""One-shot jobs (DESIGN.md 4.5, 5.5): make targets and recipes, persisted with their log.

A job is either a command (``make -s ...`` in its own session, output to the job's log) or a
coroutine that writes its own log lines (recipes, in-process steps). Records live at
``.temporal/console/jobs/<id>.json``; a console restart adopts running ones by process group.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from demo_console.events import EventBus
from demo_console.payloads import iso, now
from demo_console.procs import tail_file
from demo_console.runner import CommandRunner
from demo_console.settings import Settings

LOG_TAIL = 200
PUSH_INTERVAL_S = 0.25  # at most 4 job events per second


@dataclass
class Job:
    id: str
    action: str
    command: str
    cost: str
    started_at: str
    status: str = "running"
    ended_at: str | None = None
    exit_code: int | None = None
    result: Any = None
    params: dict[str, Any] = field(default_factory=dict)
    pgid: int | None = None

    def record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action,
            "command": self.command,
            "cost": self.cost,
            "started_at": self.started_at,
            "status": self.status,
            "ended_at": self.ended_at,
            "exit_code": self.exit_code,
            "result": self.result,
            "params": self.params,
            "pgid": self.pgid,
        }


class JobLog:
    """What a coroutine job writes to (one line at a time)."""

    def __init__(self, path: Path, on_line: Callable[[], None]) -> None:
        self.path = path
        self._on_line = on_line

    def line(self, text: str) -> None:
        with self.path.open("a") as f:
            for part in str(text).splitlines() or [""]:
                f.write(part + "\n")
        self._on_line()


JobBody = Callable[[JobLog], Awaitable[Any]]


class JobStore:
    def __init__(self, settings: Settings, runner: CommandRunner, bus: EventBus) -> None:
        self.settings = settings
        self.runner = runner
        self.bus = bus
        self.jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._last_push: dict[str, float] = {}
        self.dir.mkdir(parents=True, exist_ok=True)
        self._load()

    @property
    def dir(self) -> Path:
        return self.settings.state_dir / "jobs"

    def log_path(self, job_id: str) -> Path:
        return self.dir / f"{job_id}.log"

    # --- persistence ---------------------------------------------------------------------

    def _load(self) -> None:
        for path in sorted(self.dir.glob("*.json")):
            try:
                data = json.loads(path.read_text())
                job = Job(**data)
            except (ValueError, TypeError):
                continue
            self.jobs[job.id] = job

    def adopt(self) -> None:
        """Running records from before a restart: keep watching live ones, close dead ones."""
        for job in list(self.jobs.values()):
            if job.status != "running":
                continue
            if job.pgid and self.runner.group_alive(job.pgid):
                self._tasks[job.id] = asyncio.create_task(self._watch_adopted(job))
            else:
                self._finish(job, "failed", None, {"error": "the console restarted mid-job"})

    async def _watch_adopted(self, job: Job) -> None:
        while job.pgid and self.runner.group_alive(job.pgid):
            await asyncio.sleep(1.0)
            self._push(job)
        self._finish(job, "succeeded", None, {"note": "exit code unknown (adopted after restart)"})

    def _save(self, job: Job) -> None:
        (self.dir / f"{job.id}.json").write_text(json.dumps(job.record(), indent=2))

    # --- views ---------------------------------------------------------------------------

    def view(self, job: Job, lines: int = 20) -> dict[str, Any]:
        """``Job`` (DESIGN.md 5.5)."""
        return {
            "id": job.id,
            "action": job.action,
            "status": job.status,
            "command": job.command,
            "cost": job.cost,
            "started_at": job.started_at,
            "ended_at": job.ended_at,
            "exit_code": job.exit_code,
            "log_tail": tail_file(self.log_path(job.id), lines),
            "result": job.result,
        }

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def recent(self, limit: int = 20) -> list[Job]:
        return sorted(self.jobs.values(), key=lambda j: j.started_at, reverse=True)[:limit]

    def running(self, action: str | None = None) -> list[Job]:
        return [
            j
            for j in self.jobs.values()
            if j.status == "running" and (action is None or j.action == action)
        ]

    # --- running ---------------------------------------------------------------------------

    def _new_id(self, action: str) -> str:
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        base = f"j-{stamp}-{re.sub(r'[^a-z0-9-]', '-', action.lower())}"
        job_id, n = base, 2
        while job_id in self.jobs:
            job_id, n = f"{base}-{n}", n + 1
        return job_id

    def _push(self, job: Job, *, force: bool = False) -> None:
        loop_time = asyncio.get_running_loop().time()
        if not force and loop_time - self._last_push.get(job.id, 0.0) < PUSH_INTERVAL_S:
            return
        self._last_push[job.id] = loop_time
        self.bus.publish("job", self.view(job))

    def _finish(self, job: Job, status: str, exit_code: int | None, result: Any) -> None:
        job.status = status
        job.exit_code = exit_code
        job.ended_at = iso(now())
        if result is not None:
            job.result = result
        self._save(job)
        self.bus.publish("job", self.view(job))

    def _create(
        self, action: str, command: str, cost: str, params: Mapping[str, Any] | None
    ) -> Job:
        job = Job(
            id=self._new_id(action),
            action=action,
            command=command,
            cost=cost,
            started_at=iso(now()) or "",
            params=dict(params or {}),
        )
        self.jobs[job.id] = job
        self.log_path(job.id).write_text(f"$ {command}\n")
        self._save(job)
        self._push(job, force=True)
        return job

    async def run_command(
        self,
        action: str,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cost: str = "free",
        params: Mapping[str, Any] | None = None,
        parse: Callable[[str], Any] | None = None,
        display: str | None = None,
    ) -> Job:
        """Start ``argv`` as a job and return at once (``202``)."""
        job = self._create(action, display or " ".join(argv), cost, params)
        spawned = await self.runner.spawn(
            list(argv), env=env, cwd=self.settings.root, log_path=self.log_path(job.id)
        )
        job.pgid = spawned.pid
        self._save(job)

        async def follow() -> None:
            waiter = asyncio.ensure_future(spawned.wait())
            size = -1
            while not waiter.done():
                await asyncio.sleep(PUSH_INTERVAL_S)
                new = self.log_path(job.id).stat().st_size
                if new != size:
                    size = new
                    self._push(job)
            code = waiter.result()
            text = "\n".join(tail_file(self.log_path(job.id), 2000))
            result = None
            if parse is not None:
                with contextlib.suppress(Exception):
                    result = parse(text)
            self._finish(job, "succeeded" if code == 0 else "failed", code, result)

        self._tasks[job.id] = asyncio.create_task(follow())
        return job

    async def run_body(
        self,
        action: str,
        command: str,
        body: JobBody,
        *,
        cost: str = "free",
        params: Mapping[str, Any] | None = None,
    ) -> Job:
        """Run a coroutine as a job (recipes); its return value is the job's ``result``."""
        job = self._create(action, command, cost, params)
        log = JobLog(self.log_path(job.id), lambda: self._push(job))

        async def go() -> None:
            try:
                result = await body(log)
            except Exception as err:
                log.line(f"failed: {err}")
                self._finish(job, "failed", 1, {"error": str(err)})
                return
            self._finish(job, "succeeded", 0, result)

        self._tasks[job.id] = asyncio.create_task(go())
        return job

    async def wait(self, job_id: str, within: float | None = None) -> Job:
        """Wait for a job to end (recipes chain jobs; tests)."""
        task = self._tasks.get(job_id)
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), within)
        return self.jobs[job_id]

    async def shutdown(self) -> None:
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        for task in self._tasks.values():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
