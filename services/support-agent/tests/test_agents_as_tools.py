"""Q20: why the sub-agents are fresh TemporalAgents behind a hand-written ``@tool``.

The two alternatives, run as probe Workflows (``demo_testing.probes``) with a
ScriptedModel. If a later Strands or plugin release changes either behaviour,
these tests fail and ADR-0003 should be revisited.

1. Strands' own ``Agent.as_tool()`` forwards the parent's ``cancel_signal``; the
   sub-agent mirrors it by polling ``asyncio.sleep(0.05)``, and inside a
   Workflow each sleep is a durable timer. Measured on the dev server: about one
   ``TimerStarted`` per second of sub-agent model time (a 50 ms timer fires
   about 1 s late), plus extra Workflow Tasks. On the time-skipping server the
   timers advance virtual time *while the model Activity runs*, which can time
   it out, so this test runs on the real dev server (``integration``).
2. One shared sub-agent instance (runbook 5.6's sketch builds them once in
   ``__init__``): when the model calls the tool twice in one turn, Strands
   runs both concurrently and the agent's one-invocation lock refuses the
   second (``ConcurrencyException``, returned to the model as a tool error).
"""

from __future__ import annotations

import uuid

import pytest
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from demo_testing.history import attributed_activities, count_events
from demo_testing.probes import (
    PROBE_SUB_AGENT_PROMPT,
    NativeAsToolProbeWorkflow,
    SharedSubAgentProbeWorkflow,
)
from demo_testing.scripted_model import (
    ModelCall,
    ScriptedModel,
    ToolCall,
    Turn,
    client_with_plugin,
    scripted_plugin,
)


def probe_responder(*, parallel: bool) -> ScriptedModel:
    def respond(call: ModelCall) -> Turn:
        if call.system_prompt == PROBE_SUB_AGENT_PROMPT:
            return f"sub-agent answer to {call.last_user_text!r}"
        if call.tool_results:
            return " | ".join(call.tool_results)
        if parallel:
            return [
                ToolCall("sub_agent", {"question": "a"}),
                ToolCall("sub_agent", {"question": "b"}),
            ]
        return ToolCall("sub_agent", {"input": "a"})

    # 2 s of "thinking" per model turn, like a slow real model.
    return ScriptedModel(respond, delay=2.0 if not parallel else 0.0)


@pytest.mark.integration
async def test_strands_as_tool_writes_timers_into_the_workflow_history(
    agents_client: Client,
) -> None:
    client = client_with_plugin(agents_client, scripted_plugin(probe_responder(parallel=False)))
    task_queue = f"probe-as-tool-{uuid.uuid4()}"
    async with Worker(client, task_queue=task_queue, workflows=[NativeAsToolProbeWorkflow]):
        handle = await client.start_workflow(
            NativeAsToolProbeWorkflow.run, "go", id=task_queue, task_queue=task_queue
        )
        result = await handle.result()
        history = await handle.fetch_history()
    # It works (the nested model call is attributed) ...
    assert result == "sub-agent answer to 'a'"
    assert "invoke_model [probe_sub_agent]" in attributed_activities(history)
    # ... but the cancel-signal watcher's 50 ms polls are durable timers.
    timers = count_events(history, EventType.EVENT_TYPE_TIMER_STARTED)
    assert timers >= 2, timers


@pytest.mark.workflow
async def test_one_shared_sub_agent_refuses_a_second_concurrent_call(
    time_skipping_env: WorkflowEnvironment,
) -> None:
    client = client_with_plugin(
        time_skipping_env.client, scripted_plugin(probe_responder(parallel=True))
    )
    task_queue = f"probe-shared-{uuid.uuid4()}"
    async with Worker(client, task_queue=task_queue, workflows=[SharedSubAgentProbeWorkflow]):
        result = await client.execute_workflow(
            SharedSubAgentProbeWorkflow.run, "go", id=task_queue, task_queue=task_queue
        )
    first, second = result.split(" | ")
    assert first == "sub-agent answer to 'a'"
    assert "ConcurrencyException" in second
