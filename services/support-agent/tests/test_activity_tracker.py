"""ActivityTracker: the debounce that decides when an AgentCore-shaped Worker drains (v0.7).

The decision (``remaining``) is tested with a fake clock, as the plan asks; the async
``wait_until_idle`` with short real timeouts, as samples-python's
``bedrock_agentcore/strands_agent/tests/test_agentcore_worker.py`` does.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from temporalio.worker import ActivityInboundInterceptor, ExecuteActivityInput

from support_agent.activity_tracker import ActivityTracker

pytestmark = pytest.mark.unit


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_idle_only_after_a_quiet_debounce() -> None:
    clock = FakeClock()
    tracker = ActivityTracker(clock)
    assert tracker.remaining(20) == 20
    clock.advance(19.9)
    assert not tracker.is_idle(20)
    clock.advance(0.1)
    assert tracker.is_idle(20)


def test_an_activity_in_flight_is_never_idle() -> None:
    clock = FakeClock()
    tracker = ActivityTracker(clock)
    tracker.activity_started()
    clock.advance(3600)  # a long model call: still busy
    assert tracker.remaining(20) is None
    assert not tracker.is_idle(20)


def test_start_and_finish_both_restart_the_window() -> None:
    clock = FakeClock()
    tracker = ActivityTracker(clock)
    clock.advance(15)
    tracker.activity_started()
    clock.advance(10)
    tracker.activity_finished()  # the window starts again at the finish
    assert tracker.remaining(20) == 20
    clock.advance(20)
    assert tracker.is_idle(20)
    assert (tracker.started, tracker.finished, tracker.inflight) == (1, 1, 0)


def test_overlapping_activities() -> None:
    clock = FakeClock()
    tracker = ActivityTracker(clock)
    tracker.activity_started()
    tracker.activity_started()
    tracker.activity_finished()
    clock.advance(60)
    assert tracker.remaining(20) is None  # one still running
    tracker.activity_finished()
    assert tracker.remaining(20) == 20


async def test_wait_returns_after_the_debounce() -> None:
    tracker = ActivityTracker()
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await asyncio.wait_for(tracker.wait_until_idle(0.2), timeout=2)
    assert loop.time() - t0 >= 0.19


async def test_wait_returns_at_once_when_the_fake_clock_says_idle() -> None:
    clock = FakeClock()
    tracker = ActivityTracker(clock)
    clock.advance(20)
    await asyncio.wait_for(tracker.wait_until_idle(20), timeout=0.5)


async def test_wait_blocks_while_an_activity_runs() -> None:
    tracker = ActivityTracker()
    tracker.activity_started()
    waiter = asyncio.create_task(tracker.wait_until_idle(0.1))
    await asyncio.sleep(0.4)
    assert not waiter.done()  # 4x the debounce, still busy
    tracker.activity_finished()
    await asyncio.wait_for(waiter, timeout=1)


class _Next(ActivityInboundInterceptor):
    def __init__(self, fail: bool) -> None:
        self.fail = fail

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        await asyncio.sleep(0)
        if self.fail:
            raise RuntimeError("boom")
        return "ok"


@pytest.mark.parametrize("fail", [False, True])
async def test_interceptor_counts_success_and_failure(fail: bool) -> None:
    tracker = ActivityTracker()
    inbound = tracker.intercept_activity(_Next(fail))
    if fail:
        with pytest.raises(RuntimeError):
            await inbound.execute_activity(None)  # type: ignore[arg-type]
    else:
        assert await inbound.execute_activity(None) == "ok"  # type: ignore[arg-type]
    assert (tracker.started, tracker.finished, tracker.inflight) == (1, 1, 0)
