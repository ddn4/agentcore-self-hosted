"""Live Opus 5.5: Cases A and B through the nested sub-agents (``make test-live``).

Same shape as ``test_workflow.py`` but with the Worker's pinned plugin
(``config.build_strands_plugin()``), so every agent's turns go to Bedrock.
About 14 small model calls. Skipped unless ``RUN_BEDROCK_TESTS=1``.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from temporalio.testing import WorkflowEnvironment

from demo_testing.history import attributed_activities
from demo_testing.scripted_model import client_with_plugin
from support_agent import agents, config
from support_agent.worker_factory import build_worker
from support_agent.workflows import SupportSupervisorWorkflow

pytestmark = pytest.mark.bedrock

CASES = {
    "1042": ("Order #1042, my headphones arrived broken. I want my money back.", "40"),
    "2077": ("Order #2077: my laptop arrived with a cracked screen. I want a refund.", "450"),
}


@pytest.mark.parametrize("order", sorted(CASES))
async def test_live_case_through_sub_agents(
    time_skipping_env: WorkflowEnvironment, activity_executor: ThreadPoolExecutor, order: str
) -> None:
    ticket, price = CASES[order]
    client = client_with_plugin(time_skipping_env.client, config.build_strands_plugin())
    task_queue = f"support-live-{uuid.uuid4()}"
    async with build_worker(client, task_queue=task_queue, activity_executor=activity_executor):
        handle = await client.start_workflow(
            SupportSupervisorWorkflow.run, ticket, id=task_queue, task_queue=task_queue
        )
        reply = await handle.result()
        history = await handle.fetch_history()
    attributed = set(attributed_activities(history))
    for name in (agents.SUPERVISOR_NAME, agents.ORDER_LOOKUP_NAME, agents.RETURN_POLICY_NAME):
        assert f"invoke_model [{name}]" in attributed
    assert price in reply
    assert "eligib" in reply.lower()
    if order == "2077":
        assert "escalat" in reply.lower() or "manager" in reply.lower()
