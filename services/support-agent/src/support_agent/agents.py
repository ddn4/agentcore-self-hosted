"""Team Support's agents: the Supervisor, its two sub-agents (v0.3) and its Finance tool (v0.5).

v0.5 adds the Supervisor's way to the **Finance Refund agent**, which belongs to
another team and is reached over Nexus (ADR-0003 part 2):

- ``ask_finance_agent_tool``: ``SUPPORT_A2A_MODE=tool``. An async in-workflow
  ``@tool`` that awaits the A2A ``send_message`` Nexus operation (Q19a).
- ``submit_refund_request_tool``: ``SUPPORT_A2A_MODE=handoff``. An unforced
  capture tool; the Workflow makes the Nexus call after the Supervisor's turn.

The Nexus call itself lives in ``finance_peer.py``; these factories only take a
callback, so this module stays free of Nexus code.

"Agents as tools", in-process (runbook §1.4, 5.6; ADR-0003 part 1):

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

from collections.abc import Awaitable, Callable
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

_SUPERVISOR_FACTS = """You are the Customer Support supervisor for an online store.
Use order_lookup_agent to establish the order facts, then return_policy_agent
(give it the order facts and the customer's reason) to decide eligibility.
Do not guess facts your agents can look up.
You CANNOT issue refunds. Refunds belong to the Finance Refund agent, a separate
agent owned by the Finance team. If the request is not eligible, or the order
does not exist, do not contact Finance."""

#: Runbook 5.6's SUPERVISOR_PROMPT (v0.5, ``SUPPORT_A2A_MODE=tool``): the
#: Supervisor asks Finance itself, through the ``ask_finance_agent`` tool.
SUPERVISOR_PROMPT = (
    _SUPERVISOR_FACTS
    + """
If the request is eligible, call ask_finance_agent once, with the order id, the
refund amount in USD, the customer's reason and a one-sentence summary, and wait
for the Finance agent's decision. Never promise a refund before Finance decides.
Finish with a short, friendly reply to the customer that states the item, the
price in USD and the outcome: quote Finance's refund ID (R-...) if the refund was
issued, or explain what happens next if it was not."""
)

#: Explicit-handoff fallback (runbook 5.6, ``SUPPORT_A2A_MODE=handoff``): the
#: Supervisor only *captures* the request with the unforced capture tool
#: ``submit_refund_request`` (ADR-0001 §6: Opus 5.5 refuses forced tool use, so
#: no ``structured_output_model``). The Workflow makes the Nexus call in
#: ``run()`` and hands Finance's decision back in a second message.
SUPERVISOR_HANDOFF_PROMPT = (
    _SUPERVISOR_FACTS
    + """
If the request is eligible, call submit_refund_request once, with the order id,
the refund amount in USD, the customer's reason and a one-sentence summary. It
only records the request: the Finance agent decides after you finish this turn,
so then answer with one short line saying the request is with Finance. Never
promise a refund before Finance decides. If the request is not eligible, reply
to the customer directly.
When you are given Finance's decision, write a short, friendly reply to the
customer that states the item, the price in USD and the outcome: quote Finance's
refund ID (R-...) if the refund was issued, or explain what happens next if it
was not."""
)

#: Prompt-drift guard (runbook §6): sent once if the Return Policy agent said
#: ELIGIBLE but the Supervisor finished its turn without contacting Finance.
GUARD_REPROMPT = (
    "Workflow check: the Return Policy agent found this request ELIGIBLE, but you "
    "have not contacted the Finance agent. You cannot issue refunds. Call {tool} "
    "now, as your instructions say, and then answer."
)

#: Handoff mode: the Supervisor's second message, carrying Finance's decision.
HANDOFF_DECISION = (
    "Finance's decision on the refund request you submitted: {result}\n"
    "Now write the final reply to the customer."
)


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


@dataclass(frozen=True)
class RefundRequest:
    """What the Supervisor asks Finance for (``ask_finance_agent`` / ``submit_refund_request``)."""

    order_id: str
    amount_usd: float
    reason: str
    summary: str


def ask_finance_agent_tool(ask: Callable[[RefundRequest], Awaitable[str]]) -> AgentTool:
    """``ask_finance_agent``: the A2A call to Finance as an in-workflow tool (Q19a).

    A plain async Strands ``@tool``, **not** ``activity_as_tool``: the plugin
    runs it on the Workflow event loop, so it can await the Workflow-only Nexus
    client (``ask`` ends in ``FinancePeer.ask``) for as long as Finance takes.
    """

    @tool
    async def ask_finance_agent(order_id: str, amount_usd: float, reason: str, summary: str) -> str:
        """A2A: ask the Finance team's Finance Refund agent to approve and issue a refund.

        Finance decides; this waits for its decision, which may take a long time
        (for example, a human approval). Call it at most once per ticket.

        Args:
            order_id: The order number, digits only (for example "1042").
            amount_usd: The refund amount in US dollars (the order price).
            reason: Why the customer wants the refund (for example "damaged").
            summary: One sentence for the Finance agent: what happened and what Support found.
        """
        return await ask(RefundRequest(order_id, amount_usd, reason, summary))

    return ask_finance_agent


def submit_refund_request_tool(capture: Callable[[RefundRequest], None]) -> AgentTool:
    """``submit_refund_request``: handoff mode's unforced capture tool (ADR-0001 §6).

    Records the request only; the Workflow sends it to Finance after the
    Supervisor's turn ends. No Nexus call and no I/O here.
    """

    @tool
    async def submit_refund_request(
        order_id: str, amount_usd: float, reason: str, summary: str
    ) -> str:
        """Submit a refund request for the Finance team's Finance Refund agent to decide.

        This only records the request; Finance decides after you finish this turn.

        Args:
            order_id: The order number, digits only (for example "1042").
            amount_usd: The refund amount in US dollars (the order price).
            reason: Why the customer wants the refund (for example "damaged").
            summary: One sentence for the Finance agent: what happened and what Support found.
        """
        capture(RefundRequest(order_id, amount_usd, reason, summary))
        return "Recorded. The Finance agent will decide once you finish this turn."

    return submit_refund_request


def new_supervisor(tools: list[AgentTool], system_prompt: str = SUPERVISOR_PROMPT) -> TemporalAgent:
    """The Support Supervisor: its two sub-agents plus its Finance tool (v0.5)."""
    return _agent(SUPERVISOR_NAME, system_prompt, list(tools))
