"""Why every in-workflow Strands tool in this repo is ``async def`` (found at v0.5).

Strands runs a *sync* ``@tool`` function with ``asyncio.to_thread``
(``strands/tools/decorator.py``). Inside a Workflow the event loop is
Temporal's deterministic loop, which refuses ``run_in_executor``: the tool call
fails with ``NotImplementedError`` and Strands hands that to the model as an
error tool result, so the agent carries on without the tool and nothing fails
loudly. (samples-python ``strands_plugin/tools`` uses a sync ``letter_counter``;
reported in ``docs/ask-temporal.md``.)

``ask_finance_agent`` must be async anyway, to await the Nexus operation (Q19a).
If a later Strands or plugin release runs sync tools inline, the probe below
fails and the rule can be relaxed.
"""

from __future__ import annotations

import inspect
import uuid

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from demo_testing.probes import SyncToolProbeWorkflow
from demo_testing.scripted_model import ScriptedModel, ToolCall, client_with_plugin, scripted_plugin
from support_agent import agents


@pytest.mark.unit
def test_every_supervisor_tool_is_async() -> None:
    async def ask(_req: agents.RefundRequest) -> str:
        return ""

    tools = [
        *agents.sub_agent_tools(lambda _run: None),
        agents.ask_finance_agent_tool(ask),
        agents.submit_refund_request_tool(lambda _req: None),
    ]
    for t in tools:
        assert inspect.iscoroutinefunction(t._tool_func), t.tool_name  # type: ignore[attr-defined]


@pytest.mark.workflow
async def test_a_sync_tool_fails_quietly_inside_a_workflow(
    time_skipping_env: WorkflowEnvironment,
) -> None:
    model = ScriptedModel([ToolCall("letter_counter", {"word": "banana", "letter": "a"}), "done"])
    client = client_with_plugin(time_skipping_env.client, scripted_plugin(model))
    task_queue = f"sync-tool-{uuid.uuid4()}"
    async with Worker(client, task_queue=task_queue, workflows=[SyncToolProbeWorkflow]):
        result = await client.execute_workflow(
            SyncToolProbeWorkflow.run, id=task_queue, task_queue=task_queue
        )
    assert result == "done"  # the Workflow does not fail ...
    # ... but the model never got the answer (3): it got an error result instead.
    assert model.calls[-1].tool_results == ["Error: NotImplementedError - "]
