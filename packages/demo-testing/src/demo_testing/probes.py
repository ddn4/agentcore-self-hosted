"""Test-only Workflows that probe plugin behaviour (not part of any service).

``StructuredOutputProbeWorkflow`` runs ``TemporalAgent(structured_output_model=...)``
exactly like samples-python ``strands_plugin/structured_output``, so a test can
watch, through a ScriptedModel, what the plugin asks the Worker-side model to do
when the model answers in text instead of calling the output tool.

``SyncToolProbeWorkflow`` (v0.5) runs a *sync* Strands ``@tool`` in a Workflow,
to show that it fails quietly (``test_tool_kinds.py``; ledger 53).
"""

from __future__ import annotations

from datetime import timedelta

from pydantic import BaseModel, Field
from strands import tool
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


# --- v0.3: agents as tools (Q20) ---------------------------------------------
#
# The service builds sub-agents fresh per call inside a hand-written @tool
# (support_agent.agents). These probes run the two alternatives Q20 has to rule
# out, with a ScriptedModel, so a test can show why they were not chosen.

PROBE_SUPERVISOR_PROMPT = "probe supervisor"
PROBE_SUB_AGENT_PROMPT = "probe sub-agent"


def _probe_agent(name: str, prompt: str, tools: list[object]) -> TemporalAgent:
    return TemporalAgent(
        model="bedrock",
        name=name,
        system_prompt=prompt,
        tools=tools,
        start_to_close_timeout=timedelta(seconds=30),
        retry_policy=RetryPolicy(maximum_attempts=1),
        summary=name,
        callback_handler=None,
    )


@workflow.defn
class NativeAsToolProbeWorkflow:
    """Supervisor with a sub-agent wrapped by Strands' own ``Agent.as_tool()``."""

    def __init__(self) -> None:
        sub = _probe_agent("probe_sub_agent", PROBE_SUB_AGENT_PROMPT, [])
        self.agent = _probe_agent(
            "probe_supervisor",
            PROBE_SUPERVISOR_PROMPT,
            [sub.as_tool(name="sub_agent", description="Ask the sub-agent.")],
        )

    @workflow.run
    async def run(self, prompt: str) -> str:
        return str(await self.agent.invoke_async(prompt)).strip()


@workflow.defn
class SharedSubAgentProbeWorkflow:
    """One sub-agent *instance* behind an in-workflow ``@tool`` (built once in ``__init__``).

    Runbook 5.6's sketch builds the sub-agents once; this probe shows what happens
    when the model calls that tool twice in one turn (Strands runs tool calls
    concurrently).
    """

    def __init__(self) -> None:
        sub = _probe_agent("probe_sub_agent", PROBE_SUB_AGENT_PROMPT, [])

        @tool
        async def sub_agent(question: str) -> str:
            """Ask the sub-agent.

            Args:
                question: The question.
            """
            return str(await sub.invoke_async(question)).strip()

        self.agent = _probe_agent("probe_supervisor", PROBE_SUPERVISOR_PROMPT, [sub_agent])

    @workflow.run
    async def run(self, prompt: str) -> str:
        return str(await self.agent.invoke_async(prompt)).strip()


# --- v0.5: a sync Strands @tool inside a Workflow -----------------------------------


@tool
def letter_counter(word: str, letter: str) -> int:
    """Count how many times ``letter`` appears in ``word`` (a *sync* tool)."""
    return word.count(letter)


@workflow.defn
class SyncToolProbeWorkflow:
    """samples-python ``strands_plugin/tools``' sync ``letter_counter``, on our pinned key."""

    @workflow.run
    async def run(self) -> str:
        agent = TemporalAgent(
            model="bedrock",
            tools=[letter_counter],
            start_to_close_timeout=timedelta(seconds=10),
            callback_handler=None,
        )
        return str(await agent.invoke_async("How many a's in banana?")).strip()
