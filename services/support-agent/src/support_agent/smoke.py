"""``A2ASmokeWorkflow``: the A2A hop to Finance with **no LLM** on the caller side.

Runs in namespace ``agents`` on its own unversioned queue ``nexus-smoke-tq``
(``make worker-smoke``), never on Support's agent queues (runbook 7b.9). It does
exactly what the Supervisor will do from v0.5, minus the model:

1. ``get_agent_card`` (sync Nexus operation): A2A discovery.
2. ``send_message`` (workflow-backed Nexus operation): a refund request for one
   order. The server routes it through the endpoint ``finance-agent`` into
   namespace ``agent-services``, where Finance's Worker starts the A2A task
   ``a2a-task-<this workflow id>``, and delivers the task's result back here.

Workers never talk to each other: both only dial the Temporal frontend. The
only Finance code this module imports is the published contract,
``finance_a2a_api``.

Each operation carries a **summary** (Q21: ``execute_operation(summary=...)``,
temporalio >= 1.34), so the caller's history and Web UI label the hop
"A2A → Finance Refund Agent".

Start it from the CLI (plan v0.4)::

    temporal workflow start -n agents -t nexus-smoke-tq --type A2ASmokeWorkflow \
      -w smoke-1042 --input '40'

(an optional second input picks the order: ``--input '450' --input '"2077"'``).

Shape follows samples-python ``hello_nexus/caller/workflows.py`` (Nexus client
built in ``__init__``, ``execute_operation`` in ``run``).
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from finance_a2a_api import (
        ENDPOINT,
        A2ATask,
        AgentCardRequest,
        FinanceAgentA2A,
        refund_request,
        status_text,
    )

#: Upper bound for one A2A task, end to end. ADR-0009 (v0.6) sets the caller's
#: budget above Finance's 24 h approval wait, so the caller gets an A2A
#: ``failed`` task rather than a Nexus timeout.
SEND_MESSAGE_TIMEOUT = timedelta(hours=25)
CARD_TIMEOUT = timedelta(seconds=30)


@workflow.defn
class A2ASmokeWorkflow:
    """Discover the Finance agent, ask it for one refund, return the A2A task."""

    def __init__(self) -> None:
        self.finance = workflow.create_nexus_client(service=FinanceAgentA2A, endpoint=ENDPOINT)

    @workflow.run
    async def run(self, amount_usd: float, order_id: str = "1042") -> A2ATask:
        card = await self.finance.execute_operation(
            FinanceAgentA2A.get_agent_card,
            AgentCardRequest(),
            schedule_to_close_timeout=CARD_TIMEOUT,
            summary="A2A → Finance Refund Agent: get_agent_card (discovery)",
        )
        workflow.logger.info(
            "A2A peer discovered: %s v%s (A2A %s) skills=%s",
            card.name,
            card.version,
            card.protocol_version,
            [s.id for s in card.skills],
        )
        context_id = workflow.info().workflow_id
        msg = refund_request(
            message_id=str(workflow.uuid4()),
            context_id=context_id,
            order_id=order_id,
            amount_usd=amount_usd,
            reason="damaged",
            summary=f"Smoke test from {context_id}: refund order {order_id} (arrived damaged).",
        )
        task = await self.finance.execute_operation(
            FinanceAgentA2A.send_message,
            msg,
            schedule_to_close_timeout=SEND_MESSAGE_TIMEOUT,
            summary=(
                f"A2A → Finance Refund Agent: send_message "
                f"(refund order {order_id} ${amount_usd:.2f})"
            ),
        )
        workflow.logger.info("A2A task %s is %s: %s", task.id, task.status.state, status_text(task))
        return task
