"""Why ADR-0001 rules out ``structured_output_model`` on Opus 5.5, shown offline.

When the model answers in text instead of calling the output tool, Strands
retries with a *forced* ``toolChoice``. Through the Temporal plugin that forced
request reaches the Worker-side model unchanged (the Activity input carries no
``structured_output_model``, so BedrockModel's forced -> native -> auto fallback
never runs). Opus 5.5 answers a forced ``toolChoice`` with HTTP 400, so on the
real model this path fails; the live 400 is ``spikes/opus_structured_output.py``.
"""

from __future__ import annotations

import uuid

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from demo_testing.probes import RefundRequestProbe, StructuredOutputProbeWorkflow
from demo_testing.scripted_model import (
    ScriptedModel,
    ToolCall,
    client_with_plugin,
    scripted_plugin,
)

pytestmark = pytest.mark.workflow

REFUND = {"order_id": "1042", "amount_usd": 40.0, "reason": "damaged"}


async def run_probe(env: WorkflowEnvironment, model: ScriptedModel) -> RefundRequestProbe:
    client = client_with_plugin(env.client, scripted_plugin(model))
    task_queue = f"probe-{uuid.uuid4()}"
    async with Worker(client, task_queue=task_queue, workflows=[StructuredOutputProbeWorkflow]):
        return await client.execute_workflow(
            StructuredOutputProbeWorkflow.run,
            "Order 1042, $40 headphones, damaged. Record the refund request.",
            id=f"probe-{uuid.uuid4()}",
            task_queue=task_queue,
        )


async def test_text_answer_triggers_a_forced_tool_choice_at_the_model(
    time_skipping_env: WorkflowEnvironment,
) -> None:
    model = ScriptedModel(["The refund is eligible.", ToolCall("RefundRequestProbe", REFUND)])
    result = await run_probe(time_skipping_env, model)

    assert result == RefundRequestProbe(**REFUND)
    first, retry = model.calls
    assert first.tool_choice is None
    assert first.tool_names == ["RefundRequestProbe"]
    # The retry the Worker-side model receives: forced, single tool. Opus 5.5 -> 400.
    assert retry.tool_choice == {"tool": {"name": "RefundRequestProbe"}}
    assert retry.tool_names == ["RefundRequestProbe"]


async def test_voluntary_tool_call_needs_no_forcing(time_skipping_env: WorkflowEnvironment) -> None:
    model = ScriptedModel([ToolCall("RefundRequestProbe", REFUND)])
    assert await run_probe(time_skipping_env, model) == RefundRequestProbe(**REFUND)
    assert [c.tool_choice for c in model.calls] == [None]
