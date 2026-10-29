"""A stand-in Finance peer for Support's tests: implements only the published contract.

Team Support's tests must not import team Finance's code (the contract
``finance_a2a_api`` is the only shared code, plan §2.2), yet from v0.5 every
``SupportSupervisorWorkflow`` run makes two Nexus calls to the endpoint
``finance-agent``. This module is the stub on the other end:

- :class:`StubFinanceHandler`: a ``@service_handler(service=FinanceAgentA2A)``
  with the real operation kinds: ``get_agent_card`` is a ``sync_operation``
  returning the real ``FINANCE_AGENT_CARD``; ``send_message`` is a
  ``workflow_run_operation`` starting :class:`StubFinanceTaskWorkflow` with the
  real ID scheme ``a2a-task-<context_id>``. ``fail=True`` makes ``send_message``
  fail the operation (the caller sees a ``NexusOperationError``).
- :class:`StubFinanceTaskWorkflow`: Finance's v0.4 rule without an LLM or
  payments: up to the auto-approval limit -> ``completed`` with a
  ``refund_confirmation`` artifact (refund ID ``R-STUB-<order>``), above it ->
  ``rejected``.
- :func:`stub_finance`: creates the endpoint ``finance-agent`` in the client's
  own namespace (the time-skipping test server has one namespace; Nexus works
  there, ledger item 13) and runs the stub Worker for the duration.

Cross-namespace runs with the **real** Finance Worker are in
``tests/test_a2a_nexus.py`` and ``tests/test_supervisor_a2a.py`` (start_local).

Shape follows samples-python ``hello_nexus/handler/service_handler.py`` and
``tests/hello_nexus/hello_nexus_test.py`` (endpoint created per test, deleted after).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import nexusrpc
from nexusrpc.handler import StartOperationContext, service_handler, sync_operation
from temporalio import nexus, workflow
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.worker import Worker

from demo_testing.nexus import create_nexus_endpoint, delete_nexus_endpoint

with workflow.unsafe.imports_passed_through():
    from finance_a2a_api import (
        AUTO_APPROVE_LIMIT_USD,
        ENDPOINT,
        FINANCE_AGENT_CARD,
        A2AMessage,
        A2ATask,
        AgentCard,
        AgentCardRequest,
        Artifact,
        FinanceAgentA2A,
        TaskState,
        TaskStatus,
        data_part,
        first_data,
        text_part,
    )

#: Marks stub answers, so a stub run is never mistaken for the real Finance agent.
STUB = "[stub finance]"


@workflow.defn
class StubFinanceTaskWorkflow:
    """Finance's auto-approval rule, deterministic: no model, no payments."""

    @workflow.run
    async def run(self, msg: A2AMessage) -> A2ATask:
        data = first_data(msg.parts) or {}
        order_id = str(data.get("order_id", "?"))
        amount = float(data.get("amount_usd", 0.0))
        task_id = workflow.info().workflow_id

        def status(state: TaskState, text: str) -> TaskStatus:
            reply = A2AMessage(
                role="agent",
                parts=[text_part(text)],
                message_id=str(workflow.uuid4()),
                context_id=msg.context_id,
                task_id=task_id,
            )
            return TaskStatus(state=state, message=reply)

        if amount > AUTO_APPROVE_LIMIT_USD:
            return A2ATask(
                id=task_id,
                context_id=msg.context_id,
                status=status(
                    "rejected",
                    f"{STUB} Refund of ${amount:.2f} for order {order_id} needs a Finance "
                    f"approver (auto-approval limit ${AUTO_APPROVE_LIMIT_USD:.0f}).",
                ),
            )
        refund_id = f"R-STUB-{order_id}"
        return A2ATask(
            id=task_id,
            context_id=msg.context_id,
            status=status(
                "completed",
                f"{STUB} Refund {refund_id} issued for ${amount:.2f} on order {order_id}.",
            ),
            artifacts=[
                Artifact(
                    artifact_id=f"{task_id}-refund_confirmation",
                    name="refund_confirmation",
                    parts=[
                        data_part(
                            {
                                "refund_id": refund_id,
                                "order_id": order_id,
                                "amount_usd": amount,
                                "approved_by": "auto",
                            }
                        )
                    ],
                )
            ],
        )


@service_handler(service=FinanceAgentA2A)
class StubFinanceHandler:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        #: Every ``send_message`` the stub received (for assertions).
        self.messages: list[A2AMessage] = []

    @sync_operation
    async def get_agent_card(
        self, ctx: StartOperationContext, input: AgentCardRequest
    ) -> AgentCard:
        return FINANCE_AGENT_CARD

    @nexus.workflow_run_operation
    async def send_message(
        self, ctx: nexus.WorkflowRunOperationContext, input: A2AMessage
    ) -> nexus.WorkflowHandle[A2ATask]:
        self.messages.append(input)
        if self.fail:
            raise nexusrpc.OperationError(
                f"{STUB} Finance refused the request", state=nexusrpc.OperationErrorState.FAILED
            )
        return await ctx.start_workflow(
            StubFinanceTaskWorkflow.run,
            input,
            id=f"a2a-task-{input.context_id}",
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )


@asynccontextmanager
async def stub_finance(client: Client, *, fail: bool = False) -> AsyncIterator[StubFinanceHandler]:
    """Endpoint ``finance-agent`` -> a stub Finance Worker in ``client``'s namespace."""
    task_queue = f"stub-finance-{uuid.uuid4().hex[:8]}"
    endpoint = await create_nexus_endpoint(
        client, name=ENDPOINT, target_namespace=client.namespace, task_queue=task_queue
    )
    handler = StubFinanceHandler(fail=fail)
    try:
        async with Worker(
            client,
            task_queue=task_queue,
            workflows=[StubFinanceTaskWorkflow],
            nexus_service_handlers=[handler],
        ):
            yield handler
    finally:
        await delete_nexus_endpoint(client, endpoint)
