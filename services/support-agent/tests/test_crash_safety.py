"""Crash safety: a Worker dies while the model is "thinking"; another one finishes.

Worker A's model answers the first turn (a ``lookup_order`` tool call) and then
hangs on the second turn. Worker A is shut down mid-``invoke_model``, which
cancels that Activity attempt (in-process stand-in for Ctrl-C / kill; the live
version is ``make case-a`` + killing ``make worker-support``, see the v0.2 story).

Worker B, with a fresh model and an empty Workflow cache, picks the Workflow up:
it replays the first turn and the ``lookup_order`` result from history (neither
is re-executed) and only retries the interrupted model call.

Runs on the shared two-namespace dev server (``integration``): it needs a real
server's Activity retry and Task Queue hand-off between two Workers.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from temporalio.client import Client

from demo_testing.history import activity_runs, activity_types
from demo_testing.scripted_model import Hang, ScriptedModel, ToolCall, client_with_plugin
from demo_testing.scripted_model import scripted_plugin as plugin_for
from support_agent.worker_factory import build_worker
from support_agent.workflows import SupportSupervisorWorkflow

pytestmark = [
    pytest.mark.integration,
    # Evicting a Workflow that is suspended inside Strands' agent loop (Worker A's
    # shutdown, Worker B's cache of 0) leaves Strands async generators to be
    # garbage-collected outside their event loop; Python reports that as an
    # "unraisable" GeneratorExit. Harmless: the Workflow state lives in history.
    pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning"),
]

REPLY = "Your Wireless headphones ($40.00) are eligible; Finance will review the refund."


async def test_worker_dies_mid_model_call_and_another_worker_resumes(
    agents_client: Client, activity_executor: ThreadPoolExecutor
) -> None:
    task_queue = f"support-crash-{uuid.uuid4()}"
    workflow_id = f"support-crash-{uuid.uuid4()}"

    model_a = ScriptedModel([ToolCall("lookup_order", {"order_id": "1042"}), Hang()])
    client_a = client_with_plugin(agents_client, plugin_for(model_a))
    async with build_worker(client_a, task_queue=task_queue, activity_executor=activity_executor):
        handle = await client_a.start_workflow(
            SupportSupervisorWorkflow.run,
            "Order #1042, my headphones arrived broken.",
            id=workflow_id,
            task_queue=task_queue,
        )
        # Second model turn has started and is hanging: "crash" now.
        async with asyncio.timeout(15):
            await model_a.hung.wait()
        assert len(model_a.calls) == 2
    # Worker A is gone; its in-flight invoke_model attempt was cancelled.

    model_b = ScriptedModel([ToolCall("get_return_policy", {}), REPLY])
    client_b = client_with_plugin(agents_client, plugin_for(model_b))
    async with build_worker(
        client_b,
        task_queue=task_queue,
        activity_executor=activity_executor,
        max_cached_workflows=0,  # force a full replay from history
    ):
        result = await client_b.get_workflow_handle(workflow_id).result()
        history = await handle.fetch_history()

    assert result == REPLY
    # Worker B never re-asked turn 1: its first call is the retried turn 2, which
    # already carries the lookup_order result recorded by Worker A.
    assert len(model_b.calls) == 2
    assert json.loads(model_b.calls[0].tool_results[0])["order_id"] == "1042"

    runs = activity_runs(history)
    assert activity_types(history) == [
        "invoke_model",
        "lookup_order",
        "invoke_model",
        "get_return_policy",
        "invoke_model",
    ]
    attempts = [r.attempts for r in runs]
    assert attempts == [1, 1, 2, 1, 1], attempts  # only the interrupted turn retried
    assert all(r.completed for r in runs)
