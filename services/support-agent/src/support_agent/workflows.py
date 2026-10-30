"""Workflows for team Support (caller), namespace ``agents``.

``SupportSupervisorWorkflow`` runs the Support Supervisor, a Strands agent whose
model calls are Temporal Activities (the plugin's ``invoke_model``).

- v0.2: the Supervisor called the ``lookup_order`` and ``get_return_policy``
  Activities directly (``activity_as_tool``).
- v0.3: its tools became two Support-owned **sub-agents**, the Order Lookup
  agent and the Return Policy agent (``support_agent.agents``), nested agent
  loops inside this one Workflow (Q20).
- v0.5 (this version): **the Supervisor asks Finance itself.** Before the
  Supervisor's first turn, ``run()`` discovers the Finance Refund agent's A2A
  Agent Card (sync Nexus operation ``get_agent_card``). The Supervisor then
  reaches Finance in one of two modes (``a2a_mode``, from ``SUPPORT_A2A_MODE``
  at start time, recorded in history):

  - ``tool`` (default, runbook 5.6, Q19a): ``ask_finance_agent``, an async
    in-workflow ``@tool`` that awaits the ``send_message`` Nexus operation. The
    caller history shows ``get_agent_card``, the sub-agents' ``invoke_model``
    turns, then ``NexusOperationScheduled FinanceAgentA2A.send_message`` between
    two Supervisor turns, linked to ``a2a-task-<this id>`` in ``agent-services``.
  - ``handoff`` (runbook fallback): the Supervisor captures the request with
    ``submit_refund_request`` and stops; ``run()`` makes the same Nexus call in
    plain Workflow code, then gives the Supervisor Finance's decision.

  **Prompt-drift guard** (runbook §6): if the Return Policy agent said ELIGIBLE
  but the Supervisor finished without contacting Finance, ``run()`` re-prompts it
  once. The ``a2a`` query reports the card, every exchange and the re-prompts.

Finance is **not** a child Workflow: it lives in another namespace, on another
team's Worker; the server routes the call through the endpoint ``finance-agent``.

- v0.6: Finance may answer ``input-required`` and wait hours for a human
  approver. Nothing changes in this Workflow's shape: the ``send_message``
  operation stays open (one pending Nexus operation in ``describe``) and the
  Support Worker may be stopped meanwhile; the completion wakes the Workflow
  when Finance decides. The third input ``nexus_send_timeout``
  (``SUPPORT_NEXUS_SEND_TIMEOUT``, default 25h) is the budget for that wait
  (ADR-0009). ``rejected``/``failed`` tasks and Nexus errors each come back to
  the Supervisor with an explicit next step (apology, escalation).

**Incompatible with v0.3 histories** (the command sequence changed: the first
command is now a Nexus operation). Open v0.3 runs on the unversioned dev queue
must finish or be terminated first; ``tests/histories/v0.3/`` keeps them to show
the non-determinism error. From v0.7 Worker Versioning (PINNED) handles this.

Shape follows samples-python ``strands_plugin/tools/workflow.py`` (agent built
in ``__init__``, plain ``@tool`` functions run in Workflow context,
``invoke_async`` in ``run``), ``hello_nexus/caller/workflows.py`` (Nexus client
created in the Workflow), ``strands_plugin/human_in_the_loop`` (the agent invoked
again from ``run()`` with new input: the handoff) and the temporal-developer
skill's ``@workflow.init`` pattern (state built from the start input).
"""

from __future__ import annotations

import re
from dataclasses import asdict
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.exceptions import ApplicationError

from support_agent.agents import (
    GUARD_REPROMPT,
    HANDOFF_DECISION,
    RETURN_POLICY_NAME,
    SUPERVISOR_HANDOFF_PROMPT,
    SUPERVISOR_PROMPT,
    RefundRequest,
    SubAgentRun,
    ask_finance_agent_tool,
    new_supervisor,
    sub_agent_tools,
    submit_refund_request_tool,
)
from support_agent.durations import format_duration, parse_duration
from support_agent.finance_peer import (
    A2A_MODES,
    SEND_MESSAGE_TIMEOUT,
    FinancePeer,
    card_brief,
    tool_result,
)

with workflow.unsafe.imports_passed_through():
    from finance_a2a_api import APPROVAL_WAIT_LIMIT
    from support_agent.activities import say_hello

#: An answer from the Return Policy agent that makes a refund eligible: the verdict
#: word ELIGIBLE, but not NOT_ELIGIBLE, INELIGIBLE, "NOT ELIGIBLE" or "NOT-ELIGIBLE"
#: (the prompt asks for NOT_ELIGIBLE; a live model may still write it another way).
_ELIGIBLE = re.compile(r"(?<![A-Z_])(?<!NOT )(?<!NOT-)ELIGIBLE\b")


def is_eligible(answer: str) -> bool:
    """Whether a Return Policy agent answer says ELIGIBLE (drives the prompt-drift guard)."""
    return _ELIGIBLE.search(answer) is not None


@workflow.defn
class SupportSupervisorWorkflow:
    """Support Supervisor agent plus its sub-agents; asks Finance for refunds (A2A over Nexus)."""

    @workflow.init
    def __init__(self, ticket: str, a2a_mode: str = "tool", nexus_send_timeout: str = "") -> None:
        self._mode = a2a_mode
        self._sub_agent_runs: list[SubAgentRun] = []
        self._captured: list[RefundRequest] = []
        self._guard_reprompts = 0
        # Every input is a string, so a start with fewer inputs (v0.5 histories,
        # `temporal workflow start` with two --input) still decodes correctly: the
        # SDK drops type hints when the payload count differs from the parameters.
        self._budget_error = ""
        budget = SEND_MESSAGE_TIMEOUT
        if nexus_send_timeout:
            try:
                budget = parse_duration(nexus_send_timeout)
            except ValueError as err:
                self._budget_error = str(err)
        self.finance = FinancePeer(send_timeout=budget)
        tools = sub_agent_tools(self._sub_agent_runs.append)
        if a2a_mode == "handoff":
            tools.append(submit_refund_request_tool(self._captured.append))
            self.supervisor = new_supervisor(tools, SUPERVISOR_HANDOFF_PROMPT)
        else:
            tools.append(ask_finance_agent_tool(self._ask_finance_tool))
            self.supervisor = new_supervisor(tools, SUPERVISOR_PROMPT)

    async def _ask_finance_tool(self, req: RefundRequest) -> str:
        exchange = await self.finance.ask(
            via="ask_finance_agent tool",
            order_id=req.order_id,
            amount_usd=req.amount_usd,
            reason=req.reason,
            summary=req.summary,
        )
        return tool_result(exchange)

    @workflow.run
    async def run(self, ticket: str, a2a_mode: str = "tool", nexus_send_timeout: str = "") -> str:
        if a2a_mode not in A2A_MODES:
            raise ApplicationError(
                f"a2a_mode must be one of {A2A_MODES}, got {a2a_mode!r}",
                type="InvalidA2AMode",
                non_retryable=True,
            )
        if self._budget_error:
            raise ApplicationError(
                f"nexus_send_timeout: {self._budget_error}",
                type="InvalidNexusSendTimeout",
                non_retryable=True,
            )
        budget = self.finance.send_timeout
        if budget <= APPROVAL_WAIT_LIMIT:
            # Allowed (tests and demos shorten it), but then a slow approver may show
            # up as a Nexus timeout instead of Finance's own "failed" answer (ADR-0009).
            workflow.logger.warning(
                "Nexus budget %s is not above Finance's published approval limit %s",
                format_duration(budget),
                format_duration(APPROVAL_WAIT_LIMIT),
            )
        workflow.logger.info("Support ticket received (A2A mode %s): %s", a2a_mode, ticket)
        # A2A discovery first, so the hop is visible from the first step.
        card = await self.finance.discover()
        result = await self.supervisor.invoke_async(
            f"Customer ticket: {ticket}\n\n{card_brief(card)}"
        )

        if self._eligible() and not self._contacted_finance():
            # Prompt drift: one reminder, then accept the answer (and the a2a query shows it).
            self._guard_reprompts += 1
            tool = "submit_refund_request" if a2a_mode == "handoff" else "ask_finance_agent"
            workflow.logger.warning(
                "Supervisor skipped Finance on an eligible refund; re-prompting"
            )
            result = await self.supervisor.invoke_async(GUARD_REPROMPT.format(tool=tool))
            if not self._contacted_finance():
                workflow.logger.warning("Supervisor still did not contact Finance")

        if a2a_mode == "handoff" and self._captured and not self.finance.exchanges:
            req = self._captured[0]
            exchange = await self.finance.ask(  # the A2A hop, in plain Workflow code
                via="handoff",
                order_id=req.order_id,
                amount_usd=req.amount_usd,
                reason=req.reason,
                summary=req.summary,
            )
            result = await self.supervisor.invoke_async(
                HANDOFF_DECISION.format(result=tool_result(exchange))
            )
        return str(result).strip()

    def _eligible(self) -> bool:
        return any(
            run.agent == RETURN_POLICY_NAME and is_eligible(run.answer)
            for run in self._sub_agent_runs
        )

    def _contacted_finance(self) -> bool:
        return bool(self.finance.exchanges or self._captured)

    @workflow.query
    def transcript(self) -> list[dict[str, Any]]:
        """The Supervisor's conversation so far (user, assistant and tool messages)."""
        return [dict(message) for message in self.supervisor.messages]

    @workflow.query
    def sub_agent_runs(self) -> list[dict[str, str]]:
        """Each finished sub-agent call: which agent, the question, its answer."""
        return [asdict(run) for run in self._sub_agent_runs]

    @workflow.query
    def a2a(self) -> dict[str, Any]:
        """The A2A side: mode, Finance's Agent Card, each exchange, guard re-prompts."""
        return {
            "mode": self._mode,
            "send_timeout": format_duration(self.finance.send_timeout),
            "agent_card": asdict(self.finance.card) if self.finance.card else None,
            "exchanges": [asdict(e) for e in self.finance.exchanges],
            "captured": [asdict(r) for r in self._captured],
            "guard_reprompts": self._guard_reprompts,
        }


@workflow.defn
class HelloWorkflow:
    """Trivial Workflow that proves the Worker, namespace and test harness work (v0.1)."""

    @workflow.run
    async def run(self, name: str) -> str:
        return await workflow.execute_activity(
            say_hello, name, start_to_close_timeout=timedelta(seconds=10)
        )
