"""Spike (v0.6): can the time-skipping test server skip a 24 h approval wait behind Nexus?

    uv run python spikes/nexus_long_wait_time_skipping.py    (make spike-nexus-wait; no AWS, ~30 s)

Ledger item 56. Uses the stub Finance (``demo_testing.finance_stub``: the
contract, the approval rule, no model) on ``WorkflowEnvironment.start_time_skipping()``:

1. the stub task Workflow started **directly** with $450 and a 24 h approval wait,
   nobody approving: does waiting on its result skip 24 h?
2. the same task reached **through Nexus** from ``A2ASmokeWorkflow`` (the no-LLM
   caller): does waiting on the caller's result skip the handler's 24 h?
   If not within 15 s real, try ``env.sleep(24h)``.
3. through Nexus with a 3 s wait: do the handler's timers fire in real time?

Result at v0.6 (temporalio 1.35.0): 1 skips in ~10 ms; 2 does not skip and
``env.sleep`` fails with ``RPCError: Timeout expired``; 3 takes ~3 s real. So
Supervisor-side approval tests use seconds, and the 24 h path is tested on
Finance's Workflow alone (``test_task_workflow.py``).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import timedelta

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from demo_testing.finance_stub import StubFinanceTaskWorkflow, stub_finance
from finance_a2a_api import refund_request
from support_agent.smoke import A2ASmokeWorkflow
from support_agent.smoke_worker import build_smoke_worker


def _msg(context_id: str):
    return refund_request(
        message_id="m-1",
        context_id=context_id,
        order_id="2077",
        amount_usd=450.0,
        reason="damaged",
        summary="spike",
    )


async def direct(env: WorkflowEnvironment) -> None:
    tq = f"stub-direct-{uuid.uuid4().hex[:6]}"
    async with Worker(env.client, task_queue=tq, workflows=[StubFinanceTaskWorkflow]):
        t0 = time.monotonic()
        task = await env.client.execute_workflow(
            StubFinanceTaskWorkflow.run, _msg("direct"), id="a2a-task-direct", task_queue=tq
        )
        print(f"1. direct, 24 h wait: {task.status.state} after {time.monotonic() - t0:.2f} s real")


async def via_nexus(env: WorkflowEnvironment, wait: timedelta, label: str) -> None:
    tq = f"smoke-{uuid.uuid4().hex[:6]}"
    async with (
        stub_finance(env.client, approval_timeout=wait),
        build_smoke_worker(env.client, task_queue=tq),
    ):
        wf_id = f"smoke-{uuid.uuid4().hex[:6]}"
        handle = await env.client.start_workflow(
            A2ASmokeWorkflow.run, args=[450.0, "2077"], id=wf_id, task_queue=tq
        )
        t0 = time.monotonic()
        try:
            task = await asyncio.wait_for(handle.result(), 15)
            print(f"{label}: {task.status.state} after {time.monotonic() - t0:.2f} s real")
        except TimeoutError:
            print(f"{label}: no time skip within 15 s real; trying env.sleep({wait})")
            try:
                await env.sleep(wait + timedelta(minutes=1))
                task = await asyncio.wait_for(handle.result(), 15)
                print(f"   after env.sleep: {task.status.state}")
            except Exception as err:
                print(f"   env.sleep failed: {type(err).__name__}: {err}")
            await handle.terminate("spike done")


async def main() -> None:
    env = await WorkflowEnvironment.start_time_skipping()
    try:
        await direct(env)
        await via_nexus(env, timedelta(hours=24), "2. via Nexus, 24 h wait")
        await via_nexus(env, timedelta(seconds=3), "3. via Nexus, 3 s wait")
    finally:
        await env.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
