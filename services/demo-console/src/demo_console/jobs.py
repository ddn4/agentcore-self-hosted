"""One-shot jobs (DESIGN.md 4.5, 5.5): make targets and recipes, persisted with their log.

A job is either a command (``make -s ...`` in its own session, output to the job's log) or a
coroutine that writes its own log lines (recipes, in-process steps). Records live at
``.temporal/console/jobs/<id>.json``; a console restart adopts running ones by process group.

A recipe plans a numbered checklist (``Job.steps``) and runs each step inside
``JobLog.step(id)``. A step that fails raises ``StepError``; the job's ``result`` then names
the step, the cause (one line), a hint and the last meaningful output lines.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
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
    #: What the button said ("Bring up kind"); toasts and the sheet reuse it.
    title: str | None = None
    #: A recipe's checklist: ``[{id, label, status, note, elapsed_s}]``, status
    #: ``todo | running | done | skipped | failed``.
    steps: list[dict[str, Any]] | None = None

    def record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action,
            "title": self.title,
            "steps": self.steps,
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


class StepError(Exception):
    """A recipe step failed: ``message`` is the cause in one line, ``hint`` the next move."""

    def __init__(
        self,
        message: str,
        hint: str | None = None,
        *,
        step: str | None = None,
        tail: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.step = step
        self.tail = tail or []


class JobLog:
    """What a coroutine job writes to (one line at a time), plus its checklist."""

    def __init__(
        self,
        path: Path,
        on_line: Callable[[], None],
        job: Job | None = None,
        on_save: Callable[[], None] | None = None,
    ) -> None:
        self.path = path
        self._on_line = on_line
        self._job = job
        self._on_save = on_save

    def hold(self, pgid: int | None) -> None:
        """Record the process group a streamed step runs in (``None`` when it ends).

        A console restart then adopts the live group instead of closing the job, so the
        exclusive-job check keeps blocking a second helm or kind run until it exits.
        """
        if self._job is None:
            return
        self._job.pgid = pgid
        if self._on_save is not None:
            self._on_save()

    def line(self, text: str) -> None:
        with self.path.open("a") as f:
            for part in str(text).splitlines() or [""]:
                f.write(part + "\n")
        self._on_line()

    def touch(self) -> None:
        """Something else appended to the log file (a streamed step): push the job."""
        self._on_line()

    # --- the checklist ---------------------------------------------------------------------

    @property
    def steps(self) -> list[dict[str, Any]]:
        return (self._job.steps if self._job else None) or []

    def plan(self, steps: Sequence[Mapping[str, str]]) -> None:
        items = [
            {"id": s["id"], "label": s["label"], "status": "todo", "note": None, "elapsed_s": None}
            for s in steps
        ]
        if self._job is not None:
            self._job.steps = items
        self._on_line()

    def _item(self, step_id: str) -> dict[str, Any]:
        for item in self.steps:
            if item["id"] == step_id:
                return item
        raise KeyError(f"step {step_id!r} is not in the recipe's plan")

    def skip(self, step_id: str, note: str) -> None:
        item = self._item(step_id)
        item["status"], item["note"] = "skipped", note
        self.line(f"= skip {item['label']}: {note}")

    @contextlib.contextmanager
    def step(self, step_id: str) -> Iterator[dict[str, Any]]:
        item = self._item(step_id)
        n = self.steps.index(item) + 1
        item["status"] = "running"
        self.line(f"[{n}/{len(self.steps)}] {item['label']}")
        t0 = time.monotonic()
        try:
            yield item
        except StepError as err:
            err.step = err.step or step_id
            item["status"], item["note"] = "failed", err.message
            raise
        except Exception as err:
            item["status"], item["note"] = "failed", str(err)
            raise StepError(str(err), getattr(err, "hint", None), step=step_id) from err
        else:
            if item["status"] == "running":
                item["status"] = "done"
        finally:
            item["elapsed_s"] = round(time.monotonic() - t0, 1)
            self._on_line()


JobBody = Callable[[JobLog], Awaitable[Any]]


class JobStore:
    def __init__(self, settings: Settings, runner: CommandRunner, bus: EventBus) -> None:
        self.settings = settings
        self.runner = runner
        self.bus = bus
        self.jobs: dict[str, Job] = {}
        #: Called with every job that ends (the stack probe drops its kind facts).
        self.on_finish: list[Callable[[Job], None]] = []
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
        if job.steps:
            # A recipe: only its running step outlived the restart; the rest never ran.
            step = next((s for s in job.steps if s["status"] == "running"), None)
            label = step["label"] if step else "a step"
            if step:
                step["status"], step["note"] = "failed", "the console restarted during it"
            self._finish(
                job,
                "failed",
                None,
                {
                    "error": f"the console restarted during {label}",
                    "step": step["id"] if step else None,
                    "step_label": label,
                    "hint": f"Run {job.title or job.command} again; finished steps are skipped.",
                },
            )
            return
        self._finish(job, "succeeded", None, {"note": "exit code unknown (adopted after restart)"})

    def _save(self, job: Job) -> None:
        (self.dir / f"{job.id}.json").write_text(json.dumps(job.record(), indent=2))

    # --- views ---------------------------------------------------------------------------

    def view(self, job: Job, lines: int = 20) -> dict[str, Any]:
        """``Job`` (DESIGN.md 5.5)."""
        return {
            "id": job.id,
            "action": job.action,
            "title": job.title,
            "steps": job.steps,
            "status": job.status,
            "command": job.command,
            "cost": job.cost,
            "started_at": job.started_at,
            "ended_at": job.ended_at,
            "exit_code": job.exit_code,
            "log_tail": tail_file(self.log_path(job.id), lines),
            "result": job.result,
            "params": job.params,
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
        for hook in self.on_finish:
            with contextlib.suppress(Exception):
                hook(job)

    def _create(
        self,
        action: str,
        command: str,
        cost: str,
        params: Mapping[str, Any] | None,
        title: str | None = None,
    ) -> Job:
        job = Job(
            id=self._new_id(action),
            action=action,
            command=command,
            cost=cost,
            started_at=iso(now()) or "",
            params=dict(params or {}),
            title=title,
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
        title: str | None = None,
    ) -> Job:
        """Start ``argv`` as a job and return at once (``202``)."""
        job = self._create(action, display or " ".join(argv), cost, params, title)
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
            if code != 0 and result is None:
                result = failure_of(text, f"{job.command} exited {code}")
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
        title: str | None = None,
    ) -> Job:
        """Run a coroutine as a job (recipes); its return value is the job's ``result``."""
        job = self._create(action, command, cost, params, title)
        log = JobLog(
            self.log_path(job.id), lambda: self._push(job), job, on_save=lambda: self._save(job)
        )

        async def go() -> None:
            try:
                result = await body(log)
            except StepError as err:
                label = next((s["label"] for s in log.steps if s["id"] == err.step), err.step)
                log.line(f"failed at {label}: {err.message}")
                if err.hint:
                    log.line(f"  next: {err.hint}")
                failed = {"error": err.message, "step": err.step, "step_label": label}
                failed.update({"hint": err.hint, "tail": err.tail[-8:]})
                self._finish(job, "failed", 1, failed)
                return
            except Exception as err:
                log.line(f"failed: {err}")
                result: dict[str, Any] = {"error": str(err)}
                if getattr(err, "hint", None):
                    result["hint"] = getattr(err, "hint", None)
                self._finish(job, "failed", 1, result)
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


_NOISE = re.compile(r"^(make(\[\d+\])?: \*\*\*|make(\[\d+\])?: (Entering|Leaving))")
_CAUSE = re.compile(
    r"error|refused|denied|not found|failed|cannot|timed out|x509|unknown authority|no such"
    r"|not listening|unable to",
    re.IGNORECASE,
)


def meaningful(output: str) -> list[str]:
    """Output lines without blanks and make's own ``make: *** [...] Error 1`` noise."""
    return [
        line.strip()
        for line in output.splitlines()
        if line.strip() and not _NOISE.match(line.strip()) and not line.startswith("$ ")
    ]


def cause_line(output: str) -> str | None:
    """The last line that names an error, else the last meaningful line."""
    lines = meaningful(output)
    return next((line for line in reversed(lines) if _CAUSE.search(line)), None) or (
        lines[-1] if lines else None
    )


def failure_of(output: str, fallback: str) -> dict[str, Any]:
    """A failed command job's ``result``: the cause line and the last meaningful lines."""
    return {"error": cause_line(output) or fallback, "tail": meaningful(output)[-8:]}
