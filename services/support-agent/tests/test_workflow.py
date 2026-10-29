"""SupportSupervisorWorkflow (v0.3: Supervisor + sub-agents) with a ScriptedModel.

The `workflow` layer of plan section 5: time-skipping test server, no AWS. The
ScriptedModel is registered exactly where Opus 5.5 is in production
(``StrandsPlugin(models={"bedrock": ...})``) and answers per system prompt
(``demo_testing.support_script``), since three agents share the one model key.
Test shape follows samples-python tests/strands_plugin/tools_test.py (plugin
appended to the client, ``max_cached_workflows=0`` so every Workflow Task
replays from history).
"""

from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowFailureError, WorkflowHistory
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from demo_testing.history import activity_runs, attributed_activities, count_events
from demo_testing.scripted_model import (
    ModelCall,
    ScriptedModel,
    ToolCall,
    Turn,
    client_with_plugin,
    scripted_plugin,
)
from demo_testing.support_script import SupportPrompts, support_responder
from support_agent import agents
from support_agent.worker_factory import WORKFLOWS, build_worker
from support_agent.workflows import SupportSupervisorWorkflow

pytestmark = pytest.mark.workflow

CASE_A_TICKET = "Order #1042, my headphones arrived broken. I want my money back."
CASE_B_TICKET = "Order #2077: my laptop arrived with a cracked screen. I want a refund."
PROMPTS = SupportPrompts(
    agents.SUPERVISOR_PROMPT, agents.ORDER_LOOKUP_PROMPT, agents.RETURN_POLICY_PROMPT
)
SUPERVISOR = f"invoke_model [{agents.SUPERVISOR_NAME}]"
ORDER_AGENT = f"invoke_model [{agents.ORDER_LOOKUP_NAME}]"
POLICY_AGENT = f"invoke_model [{agents.RETURN_POLICY_NAME}]"

#: Case A/B as the Web UI shows it: Supervisor turns with each sub-agent's
#: own model turns and tool Activity nested between them.
NESTED_SEQUENCE = [
    SUPERVISOR,  # -> order_lookup_agent
    ORDER_AGENT,  # -> lookup_order
    "lookup_order",
    ORDER_AGENT,  # order facts
    SUPERVISOR,  # -> return_policy_agent
    POLICY_AGENT,  # -> get_return_policy
    "get_return_policy",
    POLICY_AGENT,  # ELIGIBLE ...
    SUPERVISOR,  # reply to the customer
]


def team() -> ScriptedModel:
    return ScriptedModel(support_responder(PROMPTS))


async def run_ticket(
    env: WorkflowEnvironment,
    model: ScriptedModel,
    executor: ThreadPoolExecutor,
    ticket: str = CASE_A_TICKET,
) -> tuple[str, WorkflowHistory, Client, list[dict[str, str]]]:
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
        runs = await handle.query(SupportSupervisorWorkflow.sub_agent_runs)
    return result, history, client, runs


async def test_case_a_nests_each_sub_agent_loop_between_supervisor_turns(
    time_skipping_env: WorkflowEnvironment, activity_executor: ThreadPoolExecutor
) -> None:
    model = team()
    result, history, _, runs = await run_ticket(time_skipping_env, model, activity_executor)

    assert "$40.00" in result and "eligible" in result
    # Q20: each sub-agent's model turns are their own invoke_model Activities,
    # attributed by summary, nested between the Supervisor's turns.
    assert attributed_activities(history) == NESTED_SEQUENCE
    assert all(run.attempts == 1 and run.completed for run in activity_runs(history))
    # The hand-written @tool adds no timers (Strands' Agent.as_tool() would:
    # see test_agents_as_tools.py).
    assert count_events(history, EventType.EVENT_TYPE_TIMER_STARTED) == 0

    # The sub-agents' answers, as the sub_agent_runs query reports them.
    assert [r["agent"] for r in runs] == [agents.ORDER_LOOKUP_NAME, agents.RETURN_POLICY_NAME]
    assert runs[0]["answer"].startswith("Order 1042: Wireless headphones, $40.00")
    assert runs[1]["answer"].startswith("ELIGIBLE")
    assert "FINANCE_MANAGER_APPROVAL_REQUIRED" not in runs[1]["answer"]


async def test_each_agent_sees_only_its_own_prompt_and_tools(
    time_skipping_env: WorkflowEnvironment, activity_executor: ThreadPoolExecutor
) -> None:
    model = team()
    await run_ticket(time_skipping_env, model, activity_executor)

    by_prompt: dict[str | None, list[ModelCall]] = {}
    for call in model.calls:
        by_prompt.setdefault(call.system_prompt, []).append(call)
    assert {p: len(c) for p, c in by_prompt.items()} == {
        agents.SUPERVISOR_PROMPT: 3,
        agents.ORDER_LOOKUP_PROMPT: 2,
        agents.RETURN_POLICY_PROMPT: 2,
    }
    tools = {p: {tuple(c.tool_names) for c in calls} for p, calls in by_prompt.items()}
    assert tools[agents.SUPERVISOR_PROMPT] == {("order_lookup_agent", "return_policy_agent")}
    assert tools[agents.ORDER_LOOKUP_PROMPT] == {("lookup_order",)}
    assert tools[agents.RETURN_POLICY_PROMPT] == {("get_return_policy",)}
    # Never a forced tool_choice, for any agent (Opus 5.5 rejects it; ADR-0001).
    assert all(c.tool_choice is None for c in model.calls)

    # A sub-agent starts from an empty conversation: its first call holds only
    # the Supervisor's question, none of the Supervisor's history.
    first_order_call = by_prompt[agents.ORDER_LOOKUP_PROMPT][0]
    assert len(first_order_call.messages) == 1
    assert first_order_call.last_user_text == "Look up order 1042."
    # The Return Policy agent gets the order facts in its question and reads the
    # real policy through its own tool.
    policy_calls = by_prompt[agents.RETURN_POLICY_PROMPT]
    assert "$40.00" in policy_calls[0].last_user_text
    assert "30 days" in policy_calls[1].tool_results[0]
    # The Supervisor sees sub-agent answers as tool results, never the raw order JSON.
    last_supervisor = by_prompt[agents.SUPERVISOR_PROMPT][-1]
    assert last_supervisor.tool_results[0].startswith("Order 1042:")
    with pytest.raises(json.JSONDecodeError):
        json.loads(last_supervisor.tool_results[0])


async def test_case_b_2077_reply_says_the_request_is_escalated(
    time_skipping_env: WorkflowEnvironment, activity_executor: ThreadPoolExecutor
) -> None:
    result, history, _, runs = await run_ticket(
        time_skipping_env, team(), activity_executor, ticket=CASE_B_TICKET
    )
    assert "$450.00" in result
    assert "escalated" in result and "Finance manager" in result
    assert "FINANCE_MANAGER_APPROVAL_REQUIRED" in runs[1]["answer"]
    assert attributed_activities(history) == NESTED_SEQUENCE


async def test_unknown_order_stops_after_the_order_lookup_agent(
    time_skipping_env: WorkflowEnvironment, activity_executor: ThreadPoolExecutor
) -> None:
    result, history, _, runs = await run_ticket(
        time_skipping_env, team(), activity_executor, ticket="Order 9999 is broken."
    )
    assert "couldn't find that order" in result
    lookup = [r for r in activity_runs(history) if r.activity_type == "lookup_order"]
    # Non-retryable OrderNotFound: one attempt, reported to the sub-agent as a tool error.
    assert len(lookup) == 1 and lookup[0].attempts == 1 and not lookup[0].completed
    assert "No order with id '9999'" in runs[0]["answer"]
    assert "get_return_policy" not in [r.activity_type for r in activity_runs(history)]


async def test_two_sub_agent_calls_in_one_turn_run_concurrently(
    time_skipping_env: WorkflowEnvironment, activity_executor: ThreadPoolExecutor
) -> None:
    """A fresh sub-agent per call: parallel tool calls never share one agent.

    Strands runs a turn's tool calls concurrently. One shared sub-agent would
    refuse the second call (ConcurrencyException; see test_agents_as_tools.py).
    """
    respond = support_responder(PROMPTS)

    def two_orders(call: ModelCall) -> Turn:
        if call.system_prompt == agents.SUPERVISOR_PROMPT:
            if not call.tool_results:
                return [
                    ToolCall("order_lookup_agent", {"question": "Look up order 1042."}),
                    ToolCall("order_lookup_agent", {"question": "Look up order 2077."}),
                ]
            return " / ".join(call.tool_results)
        return respond(call)

    result, history, _, runs = await run_ticket(
        time_skipping_env,
        ScriptedModel(two_orders),
        activity_executor,
        ticket="Orders 1042 and 2077 both arrived broken.",
    )
    assert "Order 1042: Wireless headphones" in result
    assert "Order 2077: Laptop" in result
    assert [r["agent"] for r in runs] == [agents.ORDER_LOOKUP_NAME] * 2
    assert attributed_activities(history).count(ORDER_AGENT) == 4


async def test_a_failing_sub_agent_is_a_tool_error_for_the_supervisor(
    time_skipping_env: WorkflowEnvironment, activity_executor: ThreadPoolExecutor
) -> None:
    """The Return Policy agent's model Activity fails for good; the Supervisor carries on.

    Strands turns an exception raised in a ``@tool`` into an error tool result,
    so the Supervisor can apologise instead of the Workflow failing. (Workflow
    cancellation is a ``CancelledError``, which Strands does not catch.)
    """
    respond = support_responder(PROMPTS)

    def policy_agent_down(call: ModelCall) -> Turn:
        if call.system_prompt == agents.RETURN_POLICY_PROMPT:
            raise ApplicationError("model refused the request", non_retryable=True)
        if call.system_prompt == agents.SUPERVISOR_PROMPT and len(call.tool_results) == 2:
            return f"Sorry, I could not check the policy: {call.tool_results[1]}"
        return respond(call)

    result, history, _, runs = await run_ticket(
        time_skipping_env, ScriptedModel(policy_agent_down), activity_executor
    )
    assert result.startswith("Sorry, I could not check the policy: Error:")
    policy = [r for r in activity_runs(history) if r.summary == agents.RETURN_POLICY_NAME]
    assert len(policy) == 1 and not policy[0].completed
    # Only successful sub-agent runs are recorded.
    assert [r["agent"] for r in runs] == [agents.ORDER_LOOKUP_NAME]


async def test_every_model_activity_asks_for_the_pinned_bedrock_key(
    time_skipping_env: WorkflowEnvironment, activity_executor: ThreadPoolExecutor
) -> None:
    _, history, _, _ = await run_ticket(time_skipping_env, team(), activity_executor)
    scheduled = [
        e.activity_task_scheduled_event_attributes
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
        and e.activity_task_scheduled_event_attributes.activity_type.name == "invoke_model"
    ]
    model_inputs = [json.loads(a.input.payloads[0].data) for a in scheduled]
    assert len(model_inputs) == 7  # 3 Supervisor + 2 Order Lookup + 2 Return Policy
    assert {i["model_name"] for i in model_inputs} == {"bedrock"}
    assert {i["tool_choice"] for i in model_inputs} == {None}
    # Sub-agents get the same model Activity options as the Supervisor.
    assert {a.heartbeat_timeout.ToSeconds() for a in scheduled} == {10}
    assert {a.retry_policy.maximum_attempts for a in scheduled} == {5}


async def test_unknown_model_key_fails_the_model_activity(
    time_skipping_env: WorkflowEnvironment, activity_executor: ThreadPoolExecutor
) -> None:
    """With a custom models= mapping the plugin has no implicit default model.

    A Worker that registers something other than "bedrock" can never silently fall
    back to Strands' default model: the Supervisor's first invoke_model fails, is
    retried up to MODEL_RETRY.maximum_attempts, and the Workflow fails.
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


@pytest.mark.parametrize("ticket", [CASE_A_TICKET, CASE_B_TICKET])
async def test_scripted_history_replays_deterministically(
    time_skipping_env: WorkflowEnvironment, activity_executor: ThreadPoolExecutor, ticket: str
) -> None:
    _, history, client, _ = await run_ticket(
        time_skipping_env, team(), activity_executor, ticket=ticket
    )
    # Replay with the same plugin set the Worker had (sandbox passthroughs and
    # pydantic converter come from the plugin). No model is called on replay,
    # including the nested sub-agent loops.
    replayer = Replayer(workflows=WORKFLOWS, plugins=[scripted_plugin(ScriptedModel([]))])
    result = await replayer.replay_workflow(history, raise_on_replay_failure=False)
    assert result.replay_failure is None
    assert client.namespace  # keep the client alive until the end of the test


async def test_transcript_query_shows_the_supervisor_conversation(
    time_skipping_env: WorkflowEnvironment, activity_executor: ThreadPoolExecutor
) -> None:
    model = team()
    client = client_with_plugin(time_skipping_env.client, scripted_plugin(model))
    task_queue = f"support-test-{uuid.uuid4()}"
    async with build_worker(client, task_queue=task_queue, activity_executor=activity_executor):
        handle = await client.start_workflow(
            SupportSupervisorWorkflow.run,
            CASE_A_TICKET,
            id=f"support-test-{uuid.uuid4()}",
            task_queue=task_queue,
        )
        result = await handle.result()
        transcript = await handle.query(SupportSupervisorWorkflow.transcript)
    roles = [m["role"] for m in transcript]
    assert roles == ["user", "assistant", "user", "assistant", "user", "assistant"]
    tool_uses = [
        block["toolUse"]["name"]
        for message in transcript
        for block in message["content"]
        if "toolUse" in block
    ]
    assert tool_uses == ["order_lookup_agent", "return_policy_agent"]
    assert transcript[-1]["content"][0]["text"] == result
