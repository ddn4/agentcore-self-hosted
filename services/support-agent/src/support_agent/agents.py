"""Team Support's agents: the Supervisor and its two sub-agents (v0.3).

"Agents as tools", in-process (runbook §1.4, 5.6; ADR-0003):

- **Order Lookup agent**: a ``TemporalAgent`` whose only tool is the
  ``lookup_order`` Activity. Answers with the order facts.
- **Return Policy agent**: a ``TemporalAgent`` whose only tool is the
  ``get_return_policy`` Activity. Answers ``ELIGIBLE`` / ``NOT_ELIGIBLE`` and
  whether a Finance manager must approve.
- **Support Supervisor**: a ``TemporalAgent`` whose tools are two plain Strands
  ``@tool`` functions, ``order_lookup_agent`` and ``return_policy_agent``. Each
  tool runs **in Workflow context** and awaits the sub-agent's
  ``invoke_async`` (a nested agent loop). The sub-agent's model turns are its own
  ``invoke_model`` Activities, labelled with the sub-agent's name (``summary``),
  so the Web UI shows who was thinking.

Why a hand-written ``@tool`` and not Strands' ``Agent.as_tool()`` (Q20, see
``spikes/nested_agents.py``): ``as_tool()`` forwards the parent's
``cancel_signal`` to the sub-agent, and Strands mirrors it by polling with
``asyncio.sleep(0.05)``. Inside a Workflow every ``asyncio.sleep`` is a durable
timer, so the sub-agent would write a ``TimerStarted``/``TimerFired`` pair to
history every 50 ms of (time-skipped) model latency. The hand-written tool calls
``invoke_async`` without a cancel signal: Workflow cancellation still reaches
the sub-agent as ``asyncio.CancelledError`` through the awaited Activity.

Each call builds a **fresh** sub-agent: the sub-agents are stateless lookups, so
every question starts from an empty conversation (what ``as_tool(
preserve_context=False)`` does), and two calls in the same model turn (Strands
runs tool calls concurrently) never hit Strands' one-invocation-per-agent lock
(``ConcurrencyException``). Building a ``TemporalAgent`` does no I/O, so it is
safe in Workflow code.

This module is Workflow code (it builds Workflow-side agents): it is imported by
``workflows.py`` inside the sandbox; the Activities are passed through, as in
samples-python ``bedrock_agentcore/strands_agent/workflows.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.strands_agents import TemporalAgent
from temporalio.strands_agents.workflow import activity_as_tool

with workflow.unsafe.imports_passed_through():
    # Strands is not under ``temporalio.*``, so the sandbox would re-import it
    # (and urllib3/http.client behind it) unless the StrandsPlugin's
    # passthroughs are on the Worker. Passing it through here keeps this
    # module valid on any Worker, as in runbook 5.6.
    from strands import tool
    from strands.types.tools import AgentTool

    from support_agent.activities import get_return_policy, lookup_order

#: Model key pinned by ``config.build_strands_plugin()`` (ADR-0001).
MODEL_KEY = "bedrock"

#: One LLM turn. Opus 5.5 at medium effort answers in seconds; the bound is
#: generous because adaptive thinking can't be turned off on this model.
MODEL_START_TO_CLOSE = timedelta(seconds=120)
#: The plugin heartbeats at half this interval while the model streams, so a
#: Worker that dies mid-turn is noticed within ~10 s instead of after 120 s.
MODEL_HEARTBEAT = timedelta(seconds=10)
#: Temporal owns retries (botocore retries are off in the model factory).
#: Bounded, so a request the model rejects (HTTP 400) fails instead of retrying forever.
MODEL_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=20),
    maximum_attempts=5,
)
TOOL_START_TO_CLOSE = timedelta(seconds=10)

#: ``summary`` on each agent's ``invoke_model`` Activities (Web UI attribution).
SUPERVISOR_NAME = "Support Supervisor"
ORDER_LOOKUP_NAME = "Order Lookup agent"
RETURN_POLICY_NAME = "Return Policy agent"

ORDER_LOOKUP_PROMPT = """You are Support's Order Lookup agent.
Use lookup_order to find the order the question is about. Never guess.
Answer in one line: order id, item, price in USD, delivery date and days since
delivery. If the order does not exist, say so."""

RETURN_POLICY_PROMPT = """You are Support's Return Policy agent.
Read the policy with get_return_policy and decide whether the refund request in
the question is eligible. Answer with ELIGIBLE or NOT_ELIGIBLE, then one short
reason that cites the policy rule. If it is eligible and the refund is over
$100, add: FINANCE_MANAGER_APPROVAL_REQUIRED."""

#: Runbook 5.6's SUPERVISOR_PROMPT minus the Finance part (that is v0.5).
SUPERVISOR_PROMPT = """You are the Customer Support supervisor for an online store.
Use order_lookup_agent to establish the order facts, then return_policy_agent
(give it the order facts and the customer's reason) to decide eligibility.
Do not guess facts your agents can look up.
You CANNOT issue refunds yourself. If the request is eligible, tell the customer
that their refund request has been passed to the Finance team. If the Return
Policy agent says a Finance manager must approve, tell the customer the request
has been escalated for manager approval.
Finish with a short, friendly reply to the customer that states the item, the
price in USD and whether the request is eligible, and why."""


def _agent(name: str, system_prompt: str, tools: list[object]) -> TemporalAgent:
    """A TemporalAgent on the pinned model with this repo's model Activity options."""
    return TemporalAgent(
        model=MODEL_KEY,
        name=name,
        system_prompt=system_prompt,
        tools=tools,
        start_to_close_timeout=MODEL_START_TO_CLOSE,
        heartbeat_timeout=MODEL_HEARTBEAT,
        retry_policy=MODEL_RETRY,
        # Shown on each invoke_model Activity in the Web UI (attribution).
        summary=name,
        # Strands prints streamed text to stdout by default: not in a Workflow.
        callback_handler=None,
    )


def new_order_lookup_agent() -> TemporalAgent:
    """Support's Order Lookup sub-agent (fresh conversation)."""
    return _agent(
        ORDER_LOOKUP_NAME,
        ORDER_LOOKUP_PROMPT,
        [activity_as_tool(lookup_order, start_to_close_timeout=TOOL_START_TO_CLOSE)],
    )


def new_return_policy_agent() -> TemporalAgent:
    """Support's Return Policy sub-agent (fresh conversation)."""
    return _agent(
        RETURN_POLICY_NAME,
        RETURN_POLICY_PROMPT,
        [activity_as_tool(get_return_policy, start_to_close_timeout=TOOL_START_TO_CLOSE)],
    )


@dataclass
class SubAgentRun:
    """One question a sub-agent answered for the Supervisor (``sub_agent_runs`` query)."""

    agent: str
    question: str
    answer: str


def sub_agent_tools(record: Callable[[SubAgentRun], None]) -> list[AgentTool]:
    """The Supervisor's two agents-as-tools; ``record`` sees every finished run."""

    @tool
    async def order_lookup_agent(question: str) -> str:
        """Ask Support's Order Lookup agent about a customer's order.

        Args:
            question: What to look up, including the order number.
        """
        result = await new_order_lookup_agent().invoke_async(question)
        answer = str(result).strip()
        record(SubAgentRun(ORDER_LOOKUP_NAME, question, answer))
        return answer

    @tool
    async def return_policy_agent(question: str) -> str:
        """Ask Support's Return Policy agent whether a refund request is eligible.

        Args:
            question: The order facts (item, price, delivery date) and the customer's reason.
        """
        result = await new_return_policy_agent().invoke_async(question)
        answer = str(result).strip()
        record(SubAgentRun(RETURN_POLICY_NAME, question, answer))
        return answer

    return [order_lookup_agent, return_policy_agent]


def new_supervisor(tools: list[AgentTool]) -> TemporalAgent:
    """The Support Supervisor, using its sub-agents (and, from v0.5, Finance) as tools."""
    return _agent(SUPERVISOR_NAME, SUPERVISOR_PROMPT, list(tools))
