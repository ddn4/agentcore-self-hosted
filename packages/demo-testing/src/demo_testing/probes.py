"""Test-only Workflows that probe plugin behaviour (not part of any service).

``StructuredOutputProbeWorkflow`` runs ``TemporalAgent(structured_output_model=...)``
exactly like samples-python ``strands_plugin/structured_output``, so a test can
watch, through a ScriptedModel, what the plugin asks the Worker-side model to do
when the model answers in text instead of calling the output tool.
"""

from __future__ import annotations

from datetime import timedelta

from pydantic import BaseModel, Field
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.strands_agents import TemporalAgent


class RefundRequestProbe(BaseModel):
    order_id: str = Field(description="Order number")
    amount_usd: float = Field(description="Refund amount in USD")
    reason: str = Field(description="Reason for the refund")


@workflow.defn
class StructuredOutputProbeWorkflow:
    def __init__(self) -> None:
        self.agent = TemporalAgent(
            model="bedrock",
            structured_output_model=RefundRequestProbe,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=1),
            callback_handler=None,
        )

    @workflow.run
    async def run(self, prompt: str) -> RefundRequestProbe:
        result = await self.agent.invoke_async(prompt)
        assert isinstance(result.structured_output, RefundRequestProbe)
        return result.structured_output
