"""Watching an A2A task from outside: poll ``get_task`` until it reaches a state (v0.6).

Every A2A task Workflow (the real ``FinanceAgentTaskWorkflow`` and the stub in
``finance_stub``) answers the ``get_task`` query with an ``A2ATask``. Tests use
this to find the moment a task is ``input-required`` before they signal
``approve``/``reject``, as samples-python ``strands_plugin/human_in_the_loop/
run_workflow.py`` polls its ``pending_approval`` query before signalling.

Queries never advance time on the time-skipping test server (only waiting on a
result does), so polling here does not fire the approval timer.
"""

from __future__ import annotations

import asyncio
from typing import Any

from temporalio.client import Client, WorkflowHandle
from temporalio.service import RPCError

from finance_a2a_api import A2ATask


async def task_state(handle: WorkflowHandle[Any, Any]) -> A2ATask:
    """The A2A task as the ``get_task`` query returns it now."""
    return await handle.query("get_task", result_type=A2ATask)


async def wait_for_task_state(
    handle: WorkflowHandle[Any, Any], state: str, *, within: float = 30.0
) -> A2ATask:
    """Poll ``get_task`` until the task is in ``state``; fail after ``within`` real seconds."""
    deadline = asyncio.get_running_loop().time() + within
    last: str = "(no answer)"
    while True:
        try:
            task = await task_state(handle)
            last = task.status.state
            if last == state:
                return task
        except RPCError as err:  # not started yet, or no Worker has answered yet
            last = f"query failed: {err}"
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"task {handle.id} never reached {state!r}; last: {last}")
        await asyncio.sleep(0.1)


async def wait_for_a2a_task(
    client: Client, task_id: str, state: str, *, within: float = 30.0
) -> A2ATask:
    """Same, by Workflow ID (``a2a-task-<context_id>``), in ``client``'s namespace."""
    return await wait_for_task_state(client.get_workflow_handle(task_id), state, within=within)
