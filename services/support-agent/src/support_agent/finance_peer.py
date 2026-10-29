"""Support's side of the A2A hop: the Finance Refund agent as a peer, over Nexus (v0.5).

Workflow code. ``SupportSupervisorWorkflow`` owns one :class:`FinancePeer`; both
A2A modes go through it, so the hop looks the same in history whichever mode
ran:

- ``discover()``: the sync Nexus operation ``FinanceAgentA2A.get_agent_card``
  (A2A discovery). The card's skills are given to the Supervisor in its first
  message, so the model learns about Finance from Finance's own published card.
- ``ask()``: the workflow-backed Nexus operation ``FinanceAgentA2A.send_message``.
  The server routes it through the endpoint ``finance-agent`` into namespace
  ``agent-services``, where Finance's Worker starts (or joins) the A2A task
  ``a2a-task-<this workflow id>``; the task's terminal ``A2ATask`` comes back as
  the operation result. Finance is **not** a child Workflow: it lives in another
  namespace, on another team's Worker, and the only Finance code imported here is
  the published contract ``finance_a2a_api``.

``ask()`` is awaited either from the Supervisor's in-workflow ``@tool``
``ask_finance_agent`` (``tool`` mode, runbook 5.6, Q19a) or from ``run()`` after
the Supervisor has captured the request (``handoff`` mode, the runbook fallback).

A failed operation (``NexusOperationError``: Finance refused the request, the
endpoint is misconfigured, or the 25 h budget ran out) is **not** raised to the
Strands tool loop: the exchange records the error and the Supervisor is told to
apologise and not promise a refund. v0.6 adds the human approval path and
ADR-0009's timeout budget around it.

Shape follows samples-python ``hello_nexus/caller/workflows.py`` (Nexus client
created in the Workflow, ``execute_operation`` awaited) and our own
``support_agent/smoke.py`` (v0.4, the same hop without an LLM).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from temporalio import workflow
from temporalio.exceptions import NexusOperationError

with workflow.unsafe.imports_passed_through():
    from finance_a2a_api import (
        ENDPOINT,
        A2ATask,
        AgentCard,
        AgentCardRequest,
        FinanceAgentA2A,
        first_data,
        refund_request,
        status_text,
    )

#: How the Supervisor reaches Finance (``SUPPORT_A2A_MODE``, a Workflow input).
A2AMode = Literal["tool", "handoff"]
A2A_MODES: tuple[A2AMode, ...] = ("tool", "handoff")

#: The discovery call is a sync operation: Finance must answer within 10 s.
CARD_TIMEOUT = timedelta(seconds=30)
#: One A2A task, end to end. Above Finance's 24 h approval wait, so the caller
#: gets an A2A ``failed`` task rather than a Nexus timeout (plan §3.3, ADR-0009
#: at v0.6; ``SUPPORT_NEXUS_SEND_TIMEOUT`` documents the value, but Workflow
#: code can't read the environment, so it is a constant here).
SEND_MESSAGE_TIMEOUT = timedelta(hours=25)
#: Prefix of every Nexus operation summary (Web UI label of the hop, Q21).
HOP_LABEL = "A2A → Finance Refund Agent"
#: ``state`` of an exchange whose Nexus operation itself failed (not an A2A state).
UNREACHABLE = "unreachable"


@dataclass
class A2AExchange:
    """One refund request Support sent Finance, and Finance's answer (``a2a`` query)."""

    #: ``"ask_finance_agent tool"`` or ``"handoff"``: who made the Nexus call.
    via: str
    order_id: str
    amount_usd: float
    reason: str
    #: Finance's A2A task ID, ``a2a-task-<support workflow id>`` ("" if unreachable).
    task_id: str = ""
    #: A2A task state (``completed``, ``rejected``, ``failed`` …) or ``unreachable``.
    state: str = ""
    #: The Finance agent's last message on the task, or the Nexus error.
    reply: str = ""
    #: From the ``refund_confirmation`` artifact, when a refund was issued.
    refund_id: str | None = None


def tool_result(exchange: A2AExchange) -> str:
    """What the Supervisor sees as the result of asking Finance."""
    if exchange.state == UNREACHABLE:
        return (
            f"The Finance agent could not process the refund request ({exchange.reply}). "
            "No refund was issued. Do not promise one: apologise and tell the customer "
            "that a member of the Finance team will follow up."
        )
    text = f"Finance agent, A2A task {exchange.task_id}: state={exchange.state}. {exchange.reply}"
    if exchange.refund_id:
        text += f" refund_confirmation: refund_id={exchange.refund_id}."
    return text


def card_brief(card: AgentCard) -> str:
    """The Agent Card as the Supervisor reads it in its first message."""
    skills = "; ".join(f"{s.id}: {s.description}" for s in card.skills)
    return (
        f"Peer agent discovered over A2A: {card.name} v{card.version} "
        f"(owned by another team; reach it only through your Finance tool). Skills: {skills}"
    )


class FinancePeer:
    """The Finance Refund agent as seen from the Support Supervisor Workflow."""

    def __init__(self) -> None:
        self._client = workflow.create_nexus_client(service=FinanceAgentA2A, endpoint=ENDPOINT)
        self.card: AgentCard | None = None
        self.exchanges: list[A2AExchange] = []

    async def discover(self) -> AgentCard:
        """A2A discovery: ``get_agent_card`` (sync Nexus operation)."""
        self.card = await self._client.execute_operation(
            FinanceAgentA2A.get_agent_card,
            AgentCardRequest(),
            schedule_to_close_timeout=CARD_TIMEOUT,
            summary=f"{HOP_LABEL}: get_agent_card (discovery)",
        )
        workflow.logger.info(
            "A2A peer discovered: %s v%s (A2A %s) skills=%s",
            self.card.name,
            self.card.version,
            self.card.protocol_version,
            [s.id for s in self.card.skills],
        )
        return self.card

    async def ask(
        self, *, via: str, order_id: str, amount_usd: float, reason: str, summary: str
    ) -> A2AExchange:
        """A2A ``message/send``: ask Finance for a refund and wait for the task to finish."""
        context_id = workflow.info().workflow_id
        exchange = A2AExchange(via=via, order_id=order_id, amount_usd=amount_usd, reason=reason)
        msg = refund_request(
            message_id=str(workflow.uuid4()),
            context_id=context_id,
            order_id=order_id,
            amount_usd=amount_usd,
            reason=reason,
            summary=summary,
        )
        try:
            task: A2ATask = await self._client.execute_operation(  # ← the A2A hop
                FinanceAgentA2A.send_message,
                msg,
                schedule_to_close_timeout=SEND_MESSAGE_TIMEOUT,
                summary=(
                    f"{HOP_LABEL}: send_message (refund order {order_id} "
                    f"${amount_usd:.2f}) via {via}"
                ),
            )
        except NexusOperationError as err:
            cause = err.cause or err
            exchange.state = UNREACHABLE
            exchange.reply = f"{type(cause).__name__}: {cause}"
            workflow.logger.warning("A2A send_message to Finance failed: %s", exchange.reply)
        else:
            exchange.task_id = task.id
            exchange.state = task.status.state
            exchange.reply = status_text(task)
            for artifact in task.artifacts:
                data = first_data(artifact.parts)
                if artifact.name == "refund_confirmation" and data:
                    exchange.refund_id = str(data.get("refund_id"))
            workflow.logger.info("A2A task %s is %s: %s", task.id, exchange.state, exchange.reply)
        self.exchanges.append(exchange)
        return exchange
