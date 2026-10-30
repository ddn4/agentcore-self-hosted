"""Idle detection for the AgentCore-shaped Worker (runbook 5.2, plan v0.7).

A Worker Interceptor that counts Activities in flight and remembers when one last
started or finished. ``wait_until_idle(debounce)`` returns once nothing is in flight
and nothing has started or finished for ``debounce`` seconds; the caller then drains
the Worker (``graceful_shutdown_timeout``) and releases the AgentCore session
(``complete_async_task``).

Follows samples-python ``bedrock_agentcore/strands_agent/agentcore_worker.py``
(``ActivityTracker`` / ``_TrackedActivity``) and the Temporal docs page "Serverless
Workers on Amazon Bedrock AgentCore Runtime - Python SDK". Two differences, both for
testing: the clock is injectable (the plan's "debounce with a fake clock"), and the
pure ``remaining()`` holds the decision so it can be tested without an event loop.

Why Activities only, not Workflow Tasks: every model turn of every agent is an
``invoke_model`` Activity (StrandsPlugin) and every tool is an Activity or a Nexus
call, so a turn in progress always shows up here. While the Supervisor waits on
Finance (Case B) nothing runs, the Worker drains, and the session ends: the
intended behaviour (runbook 5.6 "Session draining").
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from temporalio.worker import ActivityInboundInterceptor, ExecuteActivityInput, Interceptor

log = logging.getLogger(__name__)

Clock = Callable[[], float]


class ActivityTracker(Interceptor):
    """Tracks in-flight Activities; ``wait_until_idle`` returns after a quiet debounce."""

    def __init__(self, clock: Clock = time.monotonic) -> None:
        self._clock = clock
        self.inflight = 0
        self.started = 0
        self.finished = 0
        self.last_change = clock()
        self._changed = asyncio.Event()

    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:
        return _TrackedActivity(next, self)

    # -- state changes (called by the interceptor) -------------------------------------
    def activity_started(self) -> None:
        self.inflight += 1
        self.started += 1
        self._touch()
        log.info("activity in flight: %d", self.inflight)

    def activity_finished(self) -> None:
        self.inflight -= 1
        self.finished += 1
        self._touch()

    def _touch(self) -> None:
        self.last_change = self._clock()
        self._changed.set()

    # -- the decision (pure, testable with a fake clock) -------------------------------
    def remaining(self, debounce: float) -> float | None:
        """Seconds until the Worker counts as idle; ``None`` while an Activity runs."""
        if self.inflight > 0:
            return None
        return max(0.0, debounce - (self._clock() - self.last_change))

    def is_idle(self, debounce: float) -> bool:
        return self.remaining(debounce) == 0.0

    async def wait_until_idle(self, debounce: float) -> None:
        """Return once nothing has run (started or finished) for ``debounce`` seconds."""
        while True:
            self._changed.clear()
            remaining = self.remaining(debounce)
            if remaining == 0.0:
                return
            try:
                # Wake the moment an Activity starts or finishes; a timeout means the
                # window may have passed, which remaining() re-checks.
                await asyncio.wait_for(self._changed.wait(), timeout=remaining)
            except TimeoutError:
                pass


class _TrackedActivity(ActivityInboundInterceptor):
    def __init__(self, next: ActivityInboundInterceptor, tracker: ActivityTracker) -> None:
        super().__init__(next)
        self._tracker = tracker

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        self._tracker.activity_started()
        try:
            return await self.next.execute_activity(input)
        finally:
            self._tracker.activity_finished()
