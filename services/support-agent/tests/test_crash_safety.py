"""Crash safety: a Worker dies while a *sub-agent* is "thinking"; another one finishes.

v0.3: the Supervisor's tools are nested agent loops. Worker A plays the whole
Support team (``demo_testing.support_script``) but hangs on the Return Policy
agent's first model turn, i.e. inside the Supervisor's ``return_policy_agent``
tool, two agent loops deep. Worker A is shut down mid-``invoke_model``, which
cancels that Activity attempt (in-process stand-in for Ctrl-C / kill; the live
version is ``scripts/verify/v0.3-sub-agents.sh`` step 3, a real ``kill -9``).

Worker B, with a fresh model and an empty Workflow cache, picks the Workflow up:
it replays the Supervisor's turns *and the finished Order Lookup sub-agent loop*
from history (nothing is re-executed) and only retries the interrupted model call.

Runs on the shared two-namespace dev server (``integration``): it needs a real
server's Activity retry and Task Queue hand-off between two Workers.
"""

from __future__ import annotations

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from temporalio.client import Client

from demo_testing.finance_stub import stub_finance
from demo_testing.history import activity_runs, timeline
from demo_testing.scripted_model import Hang, ModelCall, ScriptedModel, Turn, client_with_plugin
from demo_testing.scripted_model import scripted_plugin as plugin_for
from demo_testing.support_script import SupportPrompts, support_responder
from support_agent import agents
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

PROMPTS = SupportPrompts(
    agents.SUPERVISOR_PROMPT,
    agents.ORDER_LOOKUP_PROMPT,
    agents.RETURN_POLICY_PROMPT,
    agents.SUPERVISOR_HANDOFF_PROMPT,
)
CARD = "nexus FinanceAgentA2A.get_agent_card"
SEND = "nexus FinanceAgentA2A.send_message"


def hang_in_return_policy_agent() -> ScriptedModel:
    respond = support_responder(PROMPTS)

    def script(call: ModelCall) -> Turn:
        if call.system_prompt == agents.RETURN_POLICY_PROMPT:
            return Hang()
        return respond(call)

    return ScriptedModel(script)


async def test_worker_dies_mid_sub_agent_model_call_and_another_worker_resumes(
    agents_client: Client, activity_executor: ThreadPoolExecutor
) -> None:
    task_queue = f"support-crash-{uuid.uuid4()}"
    workflow_id = f"support-crash-{uuid.uuid4()}"

    async with stub_finance(agents_client) as finance:
        model_a = hang_in_return_policy_agent()
        client_a = client_with_plugin(agents_client, plugin_for(model_a))
        async with build_worker(
            client_a, task_queue=task_queue, activity_executor=activity_executor
        ):
            handle = await client_a.start_workflow(
                SupportSupervisorWorkflow.run,
                "Order #1042, my headphones arrived broken.",
                id=workflow_id,
                task_queue=task_queue,
            )
            # The Return Policy agent's first turn has started and is hanging: "crash" now.
            async with asyncio.timeout(15):
                await model_a.hung.wait()
            # Supervisor x2, Order Lookup x2, Return Policy x1 (hanging).
            assert len(model_a.calls) == 5
        # Worker A is gone; its in-flight invoke_model attempt was cancelled.

        model_b = ScriptedModel(support_responder(PROMPTS))
        client_b = client_with_plugin(agents_client, plugin_for(model_b))
        async with build_worker(
            client_b,
            task_queue=task_queue,
            activity_executor=activity_executor,
            max_cached_workflows=0,  # force a full replay from history
        ):
            result = await client_b.get_workflow_handle(workflow_id).result()
            history = await handle.fetch_history()

    assert "$40.00" in result and "R-STUB-1042" in result
    # Worker B never re-asked the Supervisor's first two turns or the Order Lookup
    # agent: its first call is the retried Return Policy turn.
    assert [c.system_prompt for c in model_b.calls] == [
        agents.RETURN_POLICY_PROMPT,
        agents.RETURN_POLICY_PROMPT,
        agents.SUPERVISOR_PROMPT,  # -> ask_finance_agent
        agents.SUPERVISOR_PROMPT,  # reply
    ]
    assert "$40.00" in model_b.calls[0].last_user_text

    runs = activity_runs(history)
    assert timeline(history) == [
        CARD,
        "invoke_model [Support Supervisor]",
        "invoke_model [Order Lookup agent]",
        "lookup_order",
        "invoke_model [Order Lookup agent]",
        "invoke_model [Support Supervisor]",
        "invoke_model [Return Policy agent]",
        "get_return_policy",
        "invoke_model [Return Policy agent]",
        "invoke_model [Support Supervisor]",
        SEND,
        "invoke_model [Support Supervisor]",
    ]
    attempts = [r.attempts for r in runs]
    assert attempts == [1, 1, 1, 1, 1, 2, 1, 1, 1, 1], attempts  # only the interrupted turn
    assert all(r.completed for r in runs)
    # Discovery ran once (Worker A) and was replayed, not re-sent, by Worker B.
    assert len(finance.messages) == 1


async def test_worker_dies_after_finance_answered_and_the_a2a_call_is_not_repeated(
    agents_client: Client, activity_executor: ThreadPoolExecutor
) -> None:
    """The kill lands in the Supervisor's last turn, after the A2A task completed.

    Worker B replays the whole conversation, *including* the send_message Nexus
    operation, from history: Finance receives exactly one message, and only the
    interrupted Supervisor turn runs again.
    """
    respond = support_responder(PROMPTS)

    def hang_in_last_turn(call: ModelCall) -> Turn:
        if call.system_prompt == agents.SUPERVISOR_PROMPT and len(call.tool_results) == 3:
            return Hang()
        return respond(call)

    task_queue = f"support-crash-{uuid.uuid4()}"
    workflow_id = f"support-crash-{uuid.uuid4()}"
    async with stub_finance(agents_client) as finance:
        model_a = ScriptedModel(hang_in_last_turn)
        client_a = client_with_plugin(agents_client, plugin_for(model_a))
        async with build_worker(
            client_a, task_queue=task_queue, activity_executor=activity_executor
        ):
            handle = await client_a.start_workflow(
                SupportSupervisorWorkflow.run,
                "Order #1042, my headphones arrived broken.",
                id=workflow_id,
                task_queue=task_queue,
            )
            async with asyncio.timeout(15):
                await model_a.hung.wait()
        model_b = ScriptedModel(respond)
        client_b = client_with_plugin(agents_client, plugin_for(model_b))
        async with build_worker(
            client_b,
            task_queue=task_queue,
            activity_executor=activity_executor,
            max_cached_workflows=0,
        ):
            result = await handle.result()
            history = await handle.fetch_history()
            a2a = await handle.query(SupportSupervisorWorkflow.a2a)

    assert "R-STUB-1042" in result
    assert len(finance.messages) == 1
    assert [c.system_prompt for c in model_b.calls] == [agents.SUPERVISOR_PROMPT]
    assert "state=completed" in model_b.calls[0].tool_results[2]
    assert timeline(history).count(SEND) == 1
    assert [e["state"] for e in a2a["exchanges"]] == ["completed"]
