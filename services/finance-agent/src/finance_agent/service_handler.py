"""The A2A "server": Finance's Nexus service handler for ``FinanceAgentA2A``.

Registered on the Finance Worker (``nexus_service_handlers=[...]``), which polls
``agent-services-tq``; the Nexus endpoint ``finance-agent`` routes there. Shape
follows samples-python ``hello_nexus/handler/service_handler.py``:

- ``get_agent_card``: a ``@nexusrpc.handler.sync_operation``. It returns the
  Agent Card directly, so it must answer well within the 10 s sync-operation
  limit (it does no I/O).
- ``send_message``: a ``@nexus.workflow_run_operation``. It starts
  ``FinanceAgentTaskWorkflow`` and returns its handle; the server delivers the
  Workflow's result (the terminal ``A2ATask``) to the caller's namespace.

**Idempotency.** The Workflow ID is ``a2a-task-<context_id>``: one A2A task per
Support conversation. Nexus retries of the same request reuse its request ID, and
``USE_EXISTING`` makes a *second* ``send_message`` for the same conversation,
while the task is open, join the running task instead of failing or starting
another one (the SDK attaches the new caller's completion callback to the
existing Workflow). After the task has closed, a resend starts a new run of the
same Workflow ID (default ``ALLOW_DUPLICATE`` reuse); no money moves twice,
because ``issue_refund`` uses the task ID as the payments idempotency key and
payments answers with the original refund (``replayed: true``).
"""

from __future__ import annotations

import re

import nexusrpc
from nexusrpc.handler import StartOperationContext, service_handler, sync_operation
from temporalio import nexus
from temporalio.common import WorkflowIDConflictPolicy

from finance_a2a_api import (
    FINANCE_AGENT_CARD,
    A2AMessage,
    A2ATask,
    AgentCard,
    AgentCardRequest,
    FinanceAgentA2A,
    first_data,
)
from finance_agent.workflows import FinanceAgentTaskWorkflow

TASK_ID_PREFIX = "a2a-task-"
#: Context IDs become part of a Workflow ID: keep them short and printable.
_CONTEXT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,199}$")


def task_id(context_id: str) -> str:
    """The A2A task (= Workflow) ID for a Support conversation."""
    return f"{TASK_ID_PREFIX}{context_id}"


@service_handler(service=FinanceAgentA2A)
class FinanceAgentA2AHandler:
    @sync_operation
    async def get_agent_card(
        self, ctx: StartOperationContext, input: AgentCardRequest
    ) -> AgentCard:
        """A2A discovery: the published Agent Card."""
        return FINANCE_AGENT_CARD

    @nexus.workflow_run_operation
    async def send_message(
        self, ctx: nexus.WorkflowRunOperationContext, input: A2AMessage
    ) -> nexus.WorkflowHandle[A2ATask]:
        """A2A ``message/send``: start (or join) the task for this conversation."""
        if input.role != "user":
            raise nexusrpc.HandlerError(
                f"send_message takes a role='user' message, got {input.role!r}",
                type=nexusrpc.HandlerErrorType.BAD_REQUEST,
            )
        if not _CONTEXT_ID.match(input.context_id):
            raise nexusrpc.HandlerError(
                f"context_id {input.context_id!r} must match {_CONTEXT_ID.pattern}",
                type=nexusrpc.HandlerErrorType.BAD_REQUEST,
            )
        data = first_data(input.parts) or {}
        amount = data.get("amount_usd")
        shown = f"${amount:.2f}" if isinstance(amount, int | float) else "$?"
        return await ctx.start_workflow(
            FinanceAgentTaskWorkflow.run,
            input,
            id=task_id(input.context_id),
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            static_summary=(
                f"A2A task from Support ({input.context_id}): refund order "
                f"{data.get('order_id', '?')} {shown}"
            ),
        )
