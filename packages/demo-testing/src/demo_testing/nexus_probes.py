"""Test-only caller Workflows for the Finance A2A endpoint (not part of any service).

``DoubleSendWorkflow`` sends the *same* A2A conversation to Finance twice, at
once (two ``send_message`` operations with one ``context_id``), to show that
Finance runs **one** task Workflow for it and both operations complete with it.
It only imports the published contract, like a real caller.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from finance_a2a_api import A2ATask, FinanceAgentA2A, refund_request


@workflow.defn
class DoubleSendWorkflow:
    @workflow.run
    async def run(self, endpoint: str, context_id: str) -> list[A2ATask]:
        finance = workflow.create_nexus_client(service=FinanceAgentA2A, endpoint=endpoint)

        async def send(n: int) -> A2ATask:
            msg = refund_request(
                message_id=f"m-{n}",
                context_id=context_id,
                order_id="1042",
                amount_usd=40.0,
                reason="damaged",
                summary=f"send #{n}",
            )
            return await finance.execute_operation(
                FinanceAgentA2A.send_message,
                msg,
                schedule_to_close_timeout=timedelta(minutes=5),
                summary=f"A2A → Finance: send_message #{n}",
            )

        return list(await asyncio.gather(send(1), send(2)))
