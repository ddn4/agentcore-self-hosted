"""Live Opus 5.5: the Supervisor, its sub-agents and the A2A call (``make test-live``).

Same shape as ``test_workflow.py`` but with the Worker's pinned plugin
(``config.build_strands_plugin()``), so every Support agent's turns go to
Bedrock. Finance is the stub peer behind the real contract (no Finance model
calls here; the cross-namespace live run is ``scripts/verify/v0.5-a2a-over-nexus.sh``).

Plan v0.5 acceptance "in 5 live runs, Finance is called every time" (prompt
drift, runbook §6): Case A in ``tool`` mode five times, recording whether the
guard had to re-prompt. Plus Case B and Case A in ``handoff`` mode once each.
About 8 small model calls per run. Skipped unless ``RUN_BEDROCK_TESTS=1``.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from temporalio.testing import WorkflowEnvironment

from demo_testing.finance_stub import stub_finance
from demo_testing.history import timeline
from demo_testing.scripted_model import client_with_plugin
from support_agent import agents, config
from support_agent.worker_factory import build_worker
from support_agent.workflows import SupportSupervisorWorkflow

pytestmark = pytest.mark.bedrock

CASE_A = "Order #1042, my headphones arrived broken. I want my money back."
CASE_B = "Order #2077: my laptop arrived with a cracked screen. I want a refund."
SEND = "nexus FinanceAgentA2A.send_message"


async def run_live(
    env: WorkflowEnvironment, executor: ThreadPoolExecutor, ticket: str, mode: str
) -> tuple[str, list[str], dict[str, object]]:
    client = client_with_plugin(env.client, config.build_strands_plugin())
    task_queue = f"support-live-{uuid.uuid4()}"
    async with (
        stub_finance(env.client),
        build_worker(client, task_queue=task_queue, activity_executor=executor),
    ):
        handle = await client.start_workflow(
            SupportSupervisorWorkflow.run, args=[ticket, mode], id=task_queue, task_queue=task_queue
        )
        reply = await handle.result()
        steps = timeline(await handle.fetch_history())
        a2a = await handle.query(SupportSupervisorWorkflow.a2a)
    print(f"\n[{mode}] guard_reprompts={a2a['guard_reprompts']} reply: {reply}")
    return reply, steps, a2a


@pytest.mark.parametrize("attempt", range(1, 6))
async def test_live_case_a_calls_finance_every_time(
    time_skipping_env: WorkflowEnvironment, activity_executor: ThreadPoolExecutor, attempt: int
) -> None:
    reply, steps, _ = await run_live(time_skipping_env, activity_executor, CASE_A, "tool")
    for name in (agents.SUPERVISOR_NAME, agents.ORDER_LOOKUP_NAME, agents.RETURN_POLICY_NAME):
        assert f"invoke_model [{name}]" in steps
    assert steps.count(SEND) == 1, f"run {attempt}: Finance was not called exactly once"
    assert "R-STUB-1042" in reply
    assert "40" in reply


async def test_live_case_a_handoff(
    time_skipping_env: WorkflowEnvironment, activity_executor: ThreadPoolExecutor
) -> None:
    reply, steps, _ = await run_live(time_skipping_env, activity_executor, CASE_A, "handoff")
    assert steps.count(SEND) == 1
    assert "R-STUB-1042" in reply


async def test_live_case_b_is_escalated(
    time_skipping_env: WorkflowEnvironment, activity_executor: ThreadPoolExecutor
) -> None:
    reply, steps, _ = await run_live(time_skipping_env, activity_executor, CASE_B, "tool")
    assert steps.count(SEND) == 1
    assert "450" in reply
    assert "escalat" in reply.lower() or "approv" in reply.lower()
