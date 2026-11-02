"""One SSE stream per page (DESIGN.md 4.1, 5.6): snapshots, emitted only when they change.

The server polls (stack every 3 s, runs every 3 s, the focused run every 1 s) through shared
caches, so several tabs cost one set of Temporal calls. Pushes (``job``, ``process``,
``alert``) come from the event bus as they happen. A ``: ping`` comment every 15 s keeps
proxies and the browser's EventSource happy. There is no replay: on reconnect the client
just renders the next snapshots.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from demo_console.clients import NotFound, TemporalUnreachable
from demo_console.events import EventBus
from demo_console.runs import RunService
from demo_console.settings import Settings
from demo_console.stack import StackProbe


@dataclass
class Intervals:
    stack: float = 3.0
    runs: float = 3.0
    run: float = 1.0
    heartbeat: float = 15.0
    tick: float = 0.25


def digest(data: Any, drop: tuple[str, ...] = ()) -> str:
    if isinstance(data, dict) and drop:
        data = {k: v for k, v in data.items() if k not in drop}
    raw = json.dumps(data, sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()


class Cached:
    """A value refreshed at most every ``ttl`` seconds, shared by every stream."""

    def __init__(self, ttl: float, factory: Callable[[], Awaitable[Any]]) -> None:
        self.ttl = ttl
        self._factory = factory
        self._value: Any = None
        self._at = 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> Any:
        async with self._lock:
            if self._value is None or time.monotonic() - self._at >= self.ttl:
                self._value = await self._factory()
                self._at = time.monotonic()
            return self._value

    def invalidate(self) -> None:
        self._at = 0.0


class StreamHub:
    def __init__(
        self,
        settings: Settings,
        stack: StackProbe,
        runs: RunService,
        bus: EventBus,
        intervals: Intervals | None = None,
    ) -> None:
        self.settings = settings
        self.stack = stack
        self.runs = runs
        self.bus = bus
        self.intervals = intervals or Intervals()
        self._ids = itertools.count(1)
        self.stack_cache = Cached(self.intervals.stack * 0.8, stack.snapshot)
        self.runs_cache = Cached(self.intervals.runs * 0.8, runs.list_runs)

    def frame(self, event: str, data: Any) -> str:
        payload = json.dumps(data, default=str, separators=(",", ":"))
        return f"id: {next(self._ids)}\nevent: {event}\ndata: {payload}\n\n"

    async def events(
        self,
        workflow_id: str | None,
        *,
        disconnected: Callable[[], Awaitable[bool]] | None = None,
    ) -> AsyncIterator[str]:
        """The stream for one page focused on ``workflow_id`` (optional)."""
        iv = self.intervals
        loop = asyncio.get_running_loop()
        hashes: dict[str, str] = {}
        due = {"stack": 0.0, "runs": 0.0, "run": 0.0}
        last_beat = loop.time()
        sent_rows: list[tuple[Any, ...]] | None = None
        temporal_down = False
        missing_run = False
        with self.bus.subscribe() as queue:
            yield "retry: 2000\n\n"
            yield self.frame("hello", self.settings.config_body())
            while True:
                if disconnected is not None and await disconnected():
                    return
                now = loop.time()
                if now >= due["stack"]:
                    due["stack"] = now + iv.stack
                    snap = await self.stack_cache.get()
                    h = digest(snap, drop=("checked_at",))
                    if hashes.get("stack") != h:
                        hashes["stack"] = h
                        yield self.frame("stack", snap)
                if now >= due["runs"]:
                    due["runs"] = now + iv.runs
                    try:
                        listing = await self.runs_cache.get()
                    except TemporalUnreachable as err:
                        if not temporal_down:
                            temporal_down = True
                            yield self.frame("alert", unreachable_alert(self.settings, str(err)))
                    else:
                        temporal_down = False
                        h = digest(listing)
                        if hashes.get("runs") != h:
                            hashes["runs"] = h
                            yield self.frame("runs", listing)
                if workflow_id and now >= due["run"]:
                    due["run"] = now + iv.run
                    try:
                        view = await self.runs.view(workflow_id)
                        rows = await self.runs.rows(workflow_id)
                    except NotFound:
                        if not missing_run:
                            missing_run = True
                            yield self.frame(
                                "alert",
                                {
                                    "level": "warn",
                                    "message": f"No run {workflow_id} in namespace agents.",
                                    "hint": "Pick a run from the list, or start a scenario.",
                                },
                            )
                    except TemporalUnreachable:
                        pass
                    else:
                        missing_run = False
                        h = digest(view)
                        if hashes.get("run") != h:
                            hashes["run"] = h
                            yield self.frame("run", view)
                        reset, new = self.runs.rows_delta(workflow_id, sent_rows, rows["rows"])
                        if reset or new:
                            sent_rows = [
                                (r.get("at"), r.get("lane"), r.get("kind"), r.get("title"))
                                for r in rows["rows"]
                            ]
                            yield self.frame(
                                "rows",
                                {
                                    "workflow_id": rows["workflow_id"],
                                    "rows_version": rows["rows_version"],
                                    "reset": reset,
                                    "rows": new,
                                },
                            )
                while not queue.empty():
                    event, data = queue.get_nowait()
                    if event in ("job", "process"):
                        self.stack_cache.invalidate()
                    yield self.frame(event, data)
                if loop.time() - last_beat >= iv.heartbeat:
                    last_beat = loop.time()
                    yield ": ping\n\n"
                try:
                    event, data = await asyncio.wait_for(queue.get(), iv.tick)
                except TimeoutError:
                    continue
                if event in ("job", "process"):
                    self.stack_cache.invalidate()
                yield self.frame(event, data)


def unreachable_alert(settings: Settings, detail: str) -> dict[str, Any]:
    if settings.profile == "local":
        hint = "Start the dev server (Manage stack), or run make dev-server-up."
    else:
        hint = f"Run make pf TEMPORAL_PROFILE={settings.profile}."
    return {
        "level": "error",
        "message": f"Temporal is not reachable at 127.0.0.1:7233 ({detail}).",
        "hint": hint,
    }
