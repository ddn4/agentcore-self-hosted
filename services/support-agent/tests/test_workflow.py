"""SupportSupervisorWorkflow (v0.5: Supervisor + sub-agents + Finance over Nexus), scripted model.

The `workflow` layer of plan section 5: time-skipping test server, no AWS. The
ScriptedModel is registered exactly where Opus 5.5 is in production
(``StrandsPlugin(models={"bedrock": ...})``) and answers per system prompt
(``demo_testing.support_script``), since several agents share the one model key.

Finance is the **stub** peer (``demo_testing.finance_stub``): it implements the
published ``FinanceAgentA2A`` contract behind the real endpoint name
``finance-agent`` in the test server's one namespace (Nexus runs on the v1
time-skipping server, ledger 13), so Support's tests never import Finance's
code. The real Finance Worker, across two namespaces, is in
``tests/test_supervisor_a2a.py``.

Test shape follows samples-python tests/strands_plugin/tools_test.py (plugin
appended to the client, ``max_cached_workflows=0`` so every Workflow Task
replays from history) and tests/hello_nexus/hello_nexus_test.py (endpoint per test).
"""

from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import pytest
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowFailureError, WorkflowHistory
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from demo_testing.finance_stub import StubFinanceHandler, stub_finance
from demo_testing.history import (
    activity_runs,
    attributed_activities,
    count_events,
    nexus_operations,
    timeline,
)
from demo_testing.scripted_model import (
    ModelCall,
    ScriptedModel,
    ToolCall,
    Turn,
    client_with_plugin,
    scripted_plugin,
)
from demo_testing.support_script import SupportPrompts, support_responder
from finance_a2a_api import ENDPOINT, SERVICE, first_data
from support_agent import agents
from support_agent.worker_factory import WORKFLOWS, build_worker
from support_agent.workflows import SupportSupervisorWorkflow

pytestmark = [
    pytest.mark.workflow,
    # max_cached_workflows=0 evicts the Workflow after every Workflow Task. From
    # v0.5 a task often ends while the Supervisor is suspended *inside* Strands'
    # agent loop (awaiting the Nexus operation in ask_finance_agent), so Strands'
    # async generators are garbage-collected outside their event loop. Python
    # reports that as an "unraisable" GeneratorExit. Harmless: the state is in
    # history and the next task replays it (same note as test_crash_safety.py).
    pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning"),
]

CASE_A_TICKET = "Order #1042, my headphones arrived broken. I want my money back."
CASE_B_TICKET = "Order #2077: my laptop arrived with a cracked screen. I want a refund."
PROMPTS = SupportPrompts(
    agents.SUPERVISOR_PROMPT,
    agents.ORDER_LOOKUP_PROMPT,
    agents.RETURN_POLICY_PROMPT,
    agents.SUPERVISOR_HANDOFF_PROMPT,
)
SUPERVISOR = f"invoke_model [{agents.SUPERVISOR_NAME}]"
ORDER_AGENT = f"invoke_model [{agents.ORDER_LOOKUP_NAME}]"
POLICY_AGENT = f"invoke_model [{agents.RETURN_POLICY_NAME}]"
CARD = f"nexus {SERVICE}.get_agent_card"
SEND = f"nexus {SERVICE}.send_message"

#: The v0.3 team, as the Web UI shows it: Supervisor turns with each sub-agent's
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
]
#: v0.5 tool mode (Q19a): discovery first; send_message *inside* the Supervisor's
#: tool call, between its "ask Finance" turn and its reply turn.
TOOL_MODE_TIMELINE = [CARD, *NESTED_SEQUENCE, SUPERVISOR, SEND, SUPERVISOR]
#: v0.5 handoff mode: the Supervisor captures the request and ends its turn; the
#: Workflow sends it; the Supervisor writes the reply from Finance's decision.
HANDOFF_TIMELINE = [CARD, *NESTED_SEQUENCE, SUPERVISOR, SUPERVISOR, SEND, SUPERVISOR]


def team() -> ScriptedModel:
    return ScriptedModel(support_responder(PROMPTS))


@dataclass
class Run:
    result: str
    history: WorkflowHistory
    client: Client
    runs: list[dict[str, str]]
    a2a: dict[str, Any]
    finance: StubFinanceHandler
    workflow_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


async def run_ticket(
    env: WorkflowEnvironment,
    model: ScriptedModel,
    executor: ThreadPoolExecutor,
    ticket: str = CASE_A_TICKET,
    mode: str = "tool",
    *,
    finance_fails: bool = False,
) -> Run:
    client = client_with_plugin(env.client, scripted_plugin(model))
    task_queue = f"support-test-{uuid.uuid4()}"
    workflow_id = f"support-test-{uuid.uuid4()}"
    async with (
        stub_finance(env.client, fail=finance_fails) as finance,
        build_worker(
            client, task_queue=task_queue, activity_executor=executor, max_cached_workflows=0
        ),
    ):
        handle = await client.start_workflow(
            SupportSupervisorWorkflow.run,
            args=[ticket, mode],
            id=workflow_id,
            task_queue=task_queue,
        )
        result = await handle.result()
        history = await handle.fetch_history()
        runs = await handle.query(SupportSupervisorWorkflow.sub_agent_runs)
        a2a = await handle.query(SupportSupervisorWorkflow.a2a)
    return Run(result, history, client, runs, a2a, finance, workflow_id)


async def test_case_a_tool_mode_asks_finance_from_inside_the_supervisor_turn(
    time_skipping_env: WorkflowEnvironment, activity_executor: ThreadPoolExecutor
) -> None:
    """Q19a: an async in-workflow @tool awaits the send_message Nexus operation."""
    run = await run_ticket(time_skipping_env, team(), activity_executor)

    assert "$40.00" in run.result and "R-STUB-1042" in run.result
    assert timeline(run.history) == TOOL_MODE_TIMELINE
    ops = nexus_operations(run.history)
    assert [(o.endpoint, o.operation, o.outcome, o.started) for o in ops] == [
        (ENDPOINT, "get_agent_card", "completed", False),
        (ENDPOINT, "send_message", "completed", True),
    ]
    assert ops[1].summary == (
        "A2A → Finance Refund Agent: send_message (refund order 1042 $40.00) "
        "via ask_finance_agent tool"
    )
    # Finance received one A2A message: this conversation, the order facts.
    (msg,) = run.finance.messages
    assert msg.role == "user" and msg.context_id == run.workflow_id
    assert first_data(msg.parts) == {"order_id": "1042", "amount_usd": 40.0, "reason": "damaged"}
    # The a2a query: the card Support discovered, and the exchange.
    assert run.a2a["mode"] == "tool" and run.a2a["guard_reprompts"] == 0
    assert run.a2a["agent_card"]["name"] == "Finance Refund Agent"
    (exchange,) = run.a2a["exchanges"]
    assert exchange["via"] == "ask_finance_agent tool"
    assert exchange["task_id"] == f"a2a-task-{run.workflow_id}"
    assert (exchange["state"], exchange["refund_id"]) == ("completed", "R-STUB-1042")
    # No timers: awaiting Nexus in a tool adds none (unlike Agent.as_tool()).
    assert count_events(run.history, EventType.EVENT_TYPE_TIMER_STARTED) == 0
    assert all(r.attempts == 1 and r.completed for r in activity_runs(run.history))


async def test_case_a_handoff_mode_gives_the_same_answer(
    time_skipping_env: WorkflowEnvironment, activity_executor: ThreadPoolExecutor
) -> None:
    """The runbook fallback: capture tool, Nexus call in run(), second Supervisor turn."""
    run = await run_ticket(time_skipping_env, team(), activity_executor, mode="handoff")

    assert "$40.00" in run.result and "R-STUB-1042" in run.result
    assert timeline(run.history) == HANDOFF_TIMELINE
    assert run.a2a["mode"] == "handoff"
    (captured,) = run.a2a["captured"]
    assert (captured["order_id"], captured["amount_usd"]) == ("1042", 40.0)
    (exchange,) = run.a2a["exchanges"]
    assert exchange["via"] == "handoff" and exchange["state"] == "completed"
    (send,) = [o for o in nexus_operations(run.history) if o.operation == "send_message"]
    assert send.summary.endswith("via handoff")
    assert len(run.finance.messages) == 1


async def test_both_modes_send_finance_the_same_a2a_message(
    time_skipping_env: WorkflowEnvironment, activity_executor: ThreadPoolExecutor
) -> None:
    tool = await run_ticket(time_skipping_env, team(), activity_executor, mode="tool")
    handoff = await run_ticket(time_skipping_env, team(), activity_executor, mode="handoff")
    (a,), (b,) = tool.finance.messages, handoff.finance.messages
    assert first_data(a.parts) == first_data(b.parts)
    assert [p.kind for p in a.parts] == [p.kind for p in b.parts] == ["text", "data"]
    # Same reply text apart from the scripted wording, same outcome.
    assert tool.a2a["exchanges"][0]["refund_id"] == handoff.a2a["exchanges"][0]["refund_id"]


async def test_supervisor_reads_the_discovered_agent_card(
    time_skipping_env: WorkflowEnvironment, activity_executor: ThreadPoolExecutor
) -> None:
    """Discovery feeds the model: the Supervisor's first message carries Finance's card."""
    model = team()
    await run_ticket(time_skipping_env, model, activity_executor)
    first = next(c for c in model.calls if c.system_prompt == agents.SUPERVISOR_PROMPT)
    assert first.last_user_text.startswith(f"Customer ticket: {CASE_A_TICKET}\n\n")
    assert "Peer agent discovered over A2A: Finance Refund Agent v1.0.0" in first.last_user_text
    assert "refund:" in first.last_user_text  # the card's skill id and description


async def test_each_agent_sees_only_its_own_prompt_and_tools(
    time_skipping_env: WorkflowEnvironment, activity_executor: ThreadPoolExecutor
) -> None:
    model = team()
    await run_ticket(time_skipping_env, model, activity_executor)

    by_prompt: dict[str | None, list[ModelCall]] = {}
    for call in model.calls:
        by_prompt.setdefault(call.system_prompt, []).append(call)
    assert {p: len(c) for p, c in by_prompt.items()} == {
        agents.SUPERVISOR_PROMPT: 4,
        agents.ORDER_LOOKUP_PROMPT: 2,
        agents.RETURN_POLICY_PROMPT: 2,
    }
    tools = {p: {tuple(c.tool_names) for c in calls} for p, calls in by_prompt.items()}
    assert tools[agents.SUPERVISOR_PROMPT] == {
        ("order_lookup_agent", "return_policy_agent", "ask_finance_agent")
    }
    assert tools[agents.ORDER_LOOKUP_PROMPT] == {("lookup_order",)}
    assert tools[agents.RETURN_POLICY_PROMPT] == {("get_return_policy",)}
    # Never a forced tool_choice, for any agent (Opus 5.5 rejects it; ADR-0001).
    assert all(c.tool_choice is None for c in model.calls)

    # A sub-agent starts from an empty conversation: its first call holds only
    # the Supervisor's question, none of the Supervisor's history.
    first_order_call = by_prompt[agents.ORDER_LOOKUP_PROMPT][0]
    assert len(first_order_call.messages) == 1
    assert first_order_call.last_user_text == "Look up order 1042."
    policy_calls = by_prompt[agents.RETURN_POLICY_PROMPT]
    assert "$40.00" in policy_calls[0].last_user_text
    assert "30 days" in policy_calls[1].tool_results[0]
    # The Supervisor sees sub-agent answers and Finance's answer as tool results,
    # never the raw order JSON or the raw A2A task.
    last_supervisor = by_prompt[agents.SUPERVISOR_PROMPT][-1]
    assert last_supervisor.tool_results[0].startswith("Order 1042:")
    with pytest.raises(json.JSONDecodeError):
        json.loads(last_supervisor.tool_results[0])
    assert last_supervisor.tool_results[2].startswith("Finance agent, A2A task a2a-task-")
    assert "state=completed" in last_supervisor.tool_results[2]


async def test_case_b_2077_finance_rejects_and_the_reply_says_escalated(
    time_skipping_env: WorkflowEnvironment, activity_executor: ThreadPoolExecutor
) -> None:
    """$450 is over Finance's auto-approval limit: Finance rejects (v0.6 adds approval)."""
    for mode in ("tool", "handoff"):
        run = await run_ticket(
            time_skipping_env, team(), activity_executor, ticket=CASE_B_TICKET, mode=mode
        )
        assert "$450.00" in run.result
        assert "escalated" in run.result and "Finance manager" in run.result
        assert "FINANCE_MANAGER_APPROVAL_REQUIRED" in run.runs[1]["answer"]
        (exchange,) = run.a2a["exchanges"]
        assert exchange["state"] == "rejected" and exchange["refund_id"] is None
        assert SEND in timeline(run.history)


@pytest.mark.parametrize("mode", ["tool", "handoff"])
async def test_no_finance_call_when_not_eligible_or_order_unknown(
    time_skipping_env: WorkflowEnvironment, activity_executor: ThreadPoolExecutor, mode: str
) -> None:
    # 3301 was delivered 75 days ago: NOT_ELIGIBLE (rule 3).
    late = await run_ticket(
        time_skipping_env, team(), activity_executor, ticket="Order 3301 arrived broken.", mode=mode
    )
    assert "not eligible" in late.result
    assert timeline(late.history) == [CARD, *NESTED_SEQUENCE, SUPERVISOR]
    assert late.a2a["exchanges"] == [] and late.a2a["guard_reprompts"] == 0

    unknown = await run_ticket(
        time_skipping_env, team(), activity_executor, ticket="Order 9999 is broken.", mode=mode
    )
    assert "couldn't find that order" in unknown.result
    lookup = [r for r in activity_runs(unknown.history) if r.activity_type == "lookup_order"]
    # Non-retryable OrderNotFound: one attempt, reported to the sub-agent as a tool error.
    assert len(lookup) == 1 and lookup[0].attempts == 1 and not lookup[0].completed
    assert "No order with id '9999'" in unknown.runs[0]["answer"]
    assert SEND not in timeline(unknown.history)
    assert unknown.finance.messages == []


def drifting(respond_times: int) -> ScriptedModel:
    """A Supervisor that 'approves' the refund itself instead of asking Finance.

    It drifts on its first ``respond_times`` chances to call the Finance tool
    (runbook §6, "the Supervisor answers without calling Finance").
    """
    respond = support_responder(PROMPTS)
    drifts = {"left": respond_times}

    def script(call: ModelCall) -> Turn:
        turn = respond(call)
        if (
            isinstance(turn, ToolCall)
            and turn.name in ("ask_finance_agent", "submit_refund_request")
            and drifts["left"] > 0
        ):
            drifts["left"] -= 1
            return "Good news: your refund of $40.00 is approved!"
        return turn

    return ScriptedModel(script)


@pytest.mark.parametrize("mode", ["tool", "handoff"])
async def test_prompt_drift_guard_reprompts_once_and_finance_is_called(
    time_skipping_env: WorkflowEnvironment, activity_executor: ThreadPoolExecutor, mode: str
) -> None:
    model = drifting(1)
    run = await run_ticket(time_skipping_env, model, activity_executor, mode=mode)
    assert "R-STUB-1042" in run.result
    assert run.a2a["guard_reprompts"] == 1
    assert len(run.a2a["exchanges"]) == 1
    reprompts = [c for c in model.calls if c.last_user_text.startswith("Workflow check:")]
    assert reprompts and all("ELIGIBLE" in c.last_user_text for c in reprompts)
    # The drifted answer is not what the customer gets.
    assert "approved!" not in run.result


async def test_prompt_drift_guard_gives_up_after_one_reprompt(
    time_skipping_env: WorkflowEnvironment, activity_executor: ThreadPoolExecutor
) -> None:
    run = await run_ticket(time_skipping_env, drifting(2), activity_executor)
    assert run.a2a["guard_reprompts"] == 1  # exactly one reminder, no loop
    assert run.a2a["exchanges"] == [] and run.finance.messages == []
    assert SEND not in timeline(run.history)


@pytest.mark.parametrize("mode", ["tool", "handoff"])
async def test_finance_failure_is_reported_not_raised(
    time_skipping_env: WorkflowEnvironment, activity_executor: ThreadPoolExecutor, mode: str
) -> None:
    """The send_message operation fails (NexusOperationError): apologise, no refund promised."""
    run = await run_ticket(
        time_skipping_env, team(), activity_executor, mode=mode, finance_fails=True
    )
    assert "could not process the refund" in run.result and "follow up" in run.result
    (exchange,) = run.a2a["exchanges"]
    assert exchange["state"] == "unreachable" and exchange["refund_id"] is None
    assert "Finance refused the request" in exchange["reply"]
    (send,) = [o for o in nexus_operations(run.history) if o.operation == "send_message"]
    assert send.outcome == "failed"


async def test_unknown_a2a_mode_fails_the_workflow(
    time_skipping_env: WorkflowEnvironment, activity_executor: ThreadPoolExecutor
) -> None:
    with pytest.raises(WorkflowFailureError) as err:
        await run_ticket(time_skipping_env, team(), activity_executor, mode="carrier-pigeon")
    assert isinstance(err.value.cause, ApplicationError)
    assert err.value.cause.type == "InvalidA2AMode"


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

    run = await run_ticket(
        time_skipping_env,
        ScriptedModel(two_orders),
        activity_executor,
        ticket="Orders 1042 and 2077 both arrived broken.",
    )
    assert "Order 1042: Wireless headphones" in run.result
    assert "Order 2077: Laptop" in run.result
    assert [r["agent"] for r in run.runs] == [agents.ORDER_LOOKUP_NAME] * 2
    assert attributed_activities(run.history).count(ORDER_AGENT) == 4


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

    run = await run_ticket(time_skipping_env, ScriptedModel(policy_agent_down), activity_executor)
    assert run.result.startswith("Sorry, I could not check the policy: Error:")
    policy = [r for r in activity_runs(run.history) if r.summary == agents.RETURN_POLICY_NAME]
    assert len(policy) == 1 and not policy[0].completed
    # Only successful sub-agent runs are recorded; no eligibility, so no guard re-prompt.
    assert [r["agent"] for r in run.runs] == [agents.ORDER_LOOKUP_NAME]
    assert run.a2a["guard_reprompts"] == 0


async def test_every_model_activity_asks_for_the_pinned_bedrock_key(
    time_skipping_env: WorkflowEnvironment, activity_executor: ThreadPoolExecutor
) -> None:
    run = await run_ticket(time_skipping_env, team(), activity_executor)
    scheduled = [
        e.activity_task_scheduled_event_attributes
        for e in run.history.events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
        and e.activity_task_scheduled_event_attributes.activity_type.name == "invoke_model"
    ]
    model_inputs = [json.loads(a.input.payloads[0].data) for a in scheduled]
    assert len(model_inputs) == 8  # 4 Supervisor + 2 Order Lookup + 2 Return Policy
    assert {i["model_name"] for i in model_inputs} == {"bedrock"}
    assert {i["tool_choice"] for i in model_inputs} == {None}
    # Sub-agents get the same model Activity options as the Supervisor.
    assert {a.heartbeat_timeout.ToSeconds() for a in scheduled} == {10}
    assert {a.retry_policy.maximum_attempts for a in scheduled} == {5}


async def test_send_message_has_the_25h_caller_budget(
    time_skipping_env: WorkflowEnvironment, activity_executor: ThreadPoolExecutor
) -> None:
    """Plan §3.3 / ADR-0009 (v0.6): caller 25 h > Finance's 24 h approval wait."""
    run = await run_ticket(time_skipping_env, team(), activity_executor)
    timeouts = {
        e.nexus_operation_scheduled_event_attributes.operation: (
            e.nexus_operation_scheduled_event_attributes.schedule_to_close_timeout.ToSeconds()
        )
        for e in run.history.events
        if e.event_type == EventType.EVENT_TYPE_NEXUS_OPERATION_SCHEDULED
    }
    assert timeouts == {"get_agent_card": 30, "send_message": 25 * 3600}


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
    async with (
        stub_finance(time_skipping_env.client),
        build_worker(client, task_queue=task_queue, activity_executor=activity_executor),
    ):
        with pytest.raises(WorkflowFailureError) as err:
            await client.execute_workflow(
                SupportSupervisorWorkflow.run,
                CASE_A_TICKET,
                id=f"support-test-{uuid.uuid4()}",
                task_queue=task_queue,
            )
    assert "Unknown model name 'bedrock'" in str(err.value.cause.__cause__)


@pytest.mark.parametrize(
    ("ticket", "mode"),
    [(CASE_A_TICKET, "tool"), (CASE_B_TICKET, "tool"), (CASE_A_TICKET, "handoff")],
)
async def test_scripted_history_replays_deterministically(
    time_skipping_env: WorkflowEnvironment,
    activity_executor: ThreadPoolExecutor,
    ticket: str,
    mode: str,
) -> None:
    run = await run_ticket(time_skipping_env, team(), activity_executor, ticket=ticket, mode=mode)
    # Replay with the same plugin set the Worker had (sandbox passthroughs and
    # pydantic converter come from the plugin). No model and no Finance are
    # called on replay: the Nexus results come from history, like Activities.
    replayer = Replayer(workflows=WORKFLOWS, plugins=[scripted_plugin(ScriptedModel([]))])
    result = await replayer.replay_workflow(run.history, raise_on_replay_failure=False)
    assert result.replay_failure is None
    assert run.client.namespace  # keep the client alive until the end of the test


async def test_transcript_query_shows_the_supervisor_conversation(
    time_skipping_env: WorkflowEnvironment, activity_executor: ThreadPoolExecutor
) -> None:
    model = team()
    client = client_with_plugin(time_skipping_env.client, scripted_plugin(model))
    task_queue = f"support-test-{uuid.uuid4()}"
    async with (
        stub_finance(time_skipping_env.client),
        build_worker(client, task_queue=task_queue, activity_executor=activity_executor),
    ):
        handle = await client.start_workflow(
            SupportSupervisorWorkflow.run,
            CASE_A_TICKET,
            id=f"support-test-{uuid.uuid4()}",
            task_queue=task_queue,
        )
        result = await handle.result()
        transcript = await handle.query(SupportSupervisorWorkflow.transcript)
    roles = [m["role"] for m in transcript]
    assert roles == ["user", "assistant"] * 4
    tool_uses = [
        block["toolUse"]["name"]
        for message in transcript
        for block in message["content"]
        if "toolUse" in block
    ]
    assert tool_uses == ["order_lookup_agent", "return_policy_agent", "ask_finance_agent"]
    assert transcript[-1]["content"][0]["text"] == result
