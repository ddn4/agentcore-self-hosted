"""SupportSupervisorWorkflow with a ScriptedModel under the "bedrock" key.

The `workflow` layer of plan section 5: time-skipping test server, no AWS. The
ScriptedModel is registered exactly where Opus 5.5 is in production
(``StrandsPlugin(models={"bedrock": ...})``), which is the v0.2 verify item
"ScriptedModel injectable through models". Test shape follows samples-python
tests/strands_plugin/tools_test.py (plugin appended to the client,
``max_cached_workflows=0`` so every Workflow Task replays from history).
"""

from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowFailureError, WorkflowHistory
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from demo_testing.history import activity_runs, activity_types
from demo_testing.scripted_model import (
    ScriptedModel,
    ToolCall,
    client_with_plugin,
    scripted_plugin,
)
from support_agent.worker_factory import WORKFLOWS, build_worker
from support_agent.workflows import SUPERVISOR_PROMPT, SupportSupervisorWorkflow

pytestmark = pytest.mark.workflow

CASE_A_TICKET = "Order #1042, my headphones arrived broken. I want my money back."
CASE_A_REPLY = (
    "Sorry about your Wireless headphones ($40.00). Damaged items reported within "
    "30 days are eligible, so I've passed your refund request to Finance."
)


def case_a_script() -> list:
    return [
        ToolCall("lookup_order", {"order_id": "1042"}),
        ToolCall("get_return_policy", {}),
        CASE_A_REPLY,
    ]


async def run_ticket(
    env: WorkflowEnvironment,
    model: ScriptedModel,
    executor: ThreadPoolExecutor,
    ticket: str = CASE_A_TICKET,
) -> tuple[str, WorkflowHistory, Client]:
    client = client_with_plugin(env.client, scripted_plugin(model))
    task_queue = f"support-test-{uuid.uuid4()}"
    async with build_worker(
        client, task_queue=task_queue, activity_executor=executor, max_cached_workflows=0
    ):
        handle = await client.start_workflow(
            SupportSupervisorWorkflow.run,
            ticket,
            id=f"support-test-{uuid.uuid4()}",
            task_queue=task_queue,
        )
        result = await handle.result()
        history = await handle.fetch_history()
    return result, history, client


async def test_case_a_loop_interleaves_model_and_tool_activities(
    time_skipping_env: WorkflowEnvironment, activity_executor: ThreadPoolExecutor
) -> None:
    model = ScriptedModel(case_a_script())
    result, history, _ = await run_ticket(time_skipping_env, model, activity_executor)

    assert result == CASE_A_REPLY
    assert activity_types(history) == [
        "invoke_model",
        "lookup_order",
        "invoke_model",
        "get_return_policy",
        "invoke_model",
    ]
    # Each model and tool call ran exactly once (no retries in the happy path).
    assert all(run.attempts == 1 and run.completed for run in activity_runs(history))

    # The model saw the Supervisor prompt, both activity tools and never a forced
    # tool_choice (Opus 5.5 rejects "any"/"tool"; ADR-0001).
    assert [c.system_prompt for c in model.calls] == [SUPERVISOR_PROMPT] * 3
    assert all(c.tool_names == ["lookup_order", "get_return_policy"] for c in model.calls)
    assert all(c.tool_choice is None for c in model.calls)
    assert model.calls[0].last_user_text == f"Customer ticket: {CASE_A_TICKET}"

    # The tool Activities' real results were fed back to the model.
    order = json.loads(model.calls[1].tool_results[0])
    assert (order["order_id"], order["price_usd"]) == ("1042", 40.0)
    assert "30 days" in model.calls[2].tool_results[1]


async def test_every_model_activity_asks_for_the_pinned_bedrock_key(
    time_skipping_env: WorkflowEnvironment, activity_executor: ThreadPoolExecutor
) -> None:
    _, history, _ = await run_ticket(
        time_skipping_env, ScriptedModel(case_a_script()), activity_executor
    )
    model_inputs = [
        json.loads(e.activity_task_scheduled_event_attributes.input.payloads[0].data)
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
        and e.activity_task_scheduled_event_attributes.activity_type.name == "invoke_model"
    ]
    assert len(model_inputs) == 3
    assert {i["model_name"] for i in model_inputs} == {"bedrock"}
    assert {i["tool_choice"] for i in model_inputs} == {None}
    scheduled = [
        e.activity_task_scheduled_event_attributes
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
    ]
    model_attrs = next(a for a in scheduled if a.activity_type.name == "invoke_model")
    assert model_attrs.heartbeat_timeout.ToSeconds() == 10
    assert model_attrs.retry_policy.maximum_attempts == 5


async def test_unknown_order_is_a_tool_error_not_a_retry_loop(
    time_skipping_env: WorkflowEnvironment, activity_executor: ThreadPoolExecutor
) -> None:
    model = ScriptedModel(
        [
            ToolCall("lookup_order", {"order_id": "9999"}),
            "I couldn't find order 9999. Could you check the order number?",
        ]
    )
    result, history, _ = await run_ticket(
        time_skipping_env, model, activity_executor, ticket="Order 9999 is broken."
    )
    assert "couldn't find order 9999" in result
    lookup = [r for r in activity_runs(history) if r.activity_type == "lookup_order"]
    assert len(lookup) == 1 and lookup[0].attempts == 1 and not lookup[0].completed
    assert "No order with id '9999'" in model.calls[1].tool_results[0]


async def test_unknown_model_key_fails_the_model_activity(
    time_skipping_env: WorkflowEnvironment, activity_executor: ThreadPoolExecutor
) -> None:
    """With a custom models= mapping the plugin has no implicit default model.

    A Worker that registers something other than "bedrock" can never silently fall
    back to Strands' default model: the invoke_model Activity fails, is retried
    up to MODEL_RETRY.maximum_attempts, and the Workflow fails.
    """
    from temporalio.strands_agents import StrandsPlugin

    client = client_with_plugin(
        time_skipping_env.client,
        StrandsPlugin(models={"not-bedrock": lambda: ScriptedModel(["unused"])}),
    )
    task_queue = f"support-test-{uuid.uuid4()}"
    async with build_worker(client, task_queue=task_queue, activity_executor=activity_executor):
        with pytest.raises(WorkflowFailureError) as err:
            await client.execute_workflow(
                SupportSupervisorWorkflow.run,
                CASE_A_TICKET,
                id=f"support-test-{uuid.uuid4()}",
                task_queue=task_queue,
            )
    assert "Unknown model name 'bedrock'" in str(err.value.cause.__cause__)


async def test_scripted_history_replays_deterministically(
    time_skipping_env: WorkflowEnvironment, activity_executor: ThreadPoolExecutor
) -> None:
    _, history, client = await run_ticket(
        time_skipping_env, ScriptedModel(case_a_script()), activity_executor
    )
    # Replay with the same plugin set the Worker had (sandbox passthroughs and
    # pydantic converter come from the plugin). No model is called on replay.
    replayer = Replayer(workflows=WORKFLOWS, plugins=[scripted_plugin(ScriptedModel([]))])
    result = await replayer.replay_workflow(history, raise_on_replay_failure=False)
    assert result.replay_failure is None
    assert client.namespace  # keep the client alive until the end of the test


async def test_transcript_query_shows_the_conversation(
    time_skipping_env: WorkflowEnvironment, activity_executor: ThreadPoolExecutor
) -> None:
    model = ScriptedModel(case_a_script())
    client = client_with_plugin(time_skipping_env.client, scripted_plugin(model))
    task_queue = f"support-test-{uuid.uuid4()}"
    async with build_worker(client, task_queue=task_queue, activity_executor=activity_executor):
        handle = await client.start_workflow(
            SupportSupervisorWorkflow.run,
            CASE_A_TICKET,
            id=f"support-test-{uuid.uuid4()}",
            task_queue=task_queue,
        )
        await handle.result()
        transcript = await handle.query(SupportSupervisorWorkflow.transcript)
    roles = [m["role"] for m in transcript]
    assert roles == ["user", "assistant", "user", "assistant", "user", "assistant"]
    assert transcript[-1]["content"][0]["text"] == CASE_A_REPLY
