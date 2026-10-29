"""Workflows for team Support (caller), namespace ``agents``.

``SupportSupervisorWorkflow`` (v0.2) is an ordinary Strands agent whose model
calls and tool calls are all Temporal Activities:

- the model: ``TemporalAgent(model="bedrock", ...)`` runs each LLM turn as the
  plugin's ``invoke_model`` Activity on the Worker, using the model the Worker
  pinned under ``"bedrock"`` (Opus 5.5; ADR-0001);
- the tools: ``activity_as_tool(lookup_order)`` and
  ``activity_as_tool(get_return_policy)``.

If the Worker dies mid-turn, the in-flight Activity is retried on another Worker
and everything already recorded (earlier model turns, tool results) is replayed
from history, not re-executed.

Shape follows samples-python ``strands_plugin/tools/workflow.py`` (agent built in
``__init__``, ``activity_as_tool`` with its own timeout, ``invoke_async`` in
``run``) and ``bedrock_agentcore/strands_agent/workflows.py`` (system prompt,
activities imported through the sandbox pass-through).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.strands_agents import TemporalAgent
from temporalio.strands_agents.workflow import activity_as_tool

with workflow.unsafe.imports_passed_through():
    from support_agent.activities import get_return_policy, lookup_order, say_hello

#: Model key pinned by ``config.build_strands_plugin()`` (ADR-0001).
MODEL_KEY = "bedrock"

#: One LLM turn. Opus 5.5 at medium effort answers in seconds; the bound is
#: generous because adaptive thinking can't be turned off on this model.
MODEL_START_TO_CLOSE = timedelta(seconds=120)
#: The plugin heartbeats at half this interval while the model streams, so a
#: Worker that dies mid-turn is noticed within ~10 s instead of after 120 s.
MODEL_HEARTBEAT = timedelta(seconds=10)
#: Temporal owns retries (botocore retries are off in the model factory).
#: Bounded, so a request the model rejects (HTTP 400) fails the Workflow
#: instead of retrying forever.
MODEL_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=20),
    maximum_attempts=5,
)
TOOL_START_TO_CLOSE = timedelta(seconds=10)

SUPERVISOR_PROMPT = """You are the Customer Support supervisor for an online store.
Use lookup_order to find the customer's order and get_return_policy to decide
whether the request is eligible for a refund. Do not guess facts you can look up.
You CANNOT issue refunds yourself: if the request is eligible, tell the customer
that their refund request is being passed to the Finance team for approval.
Finish with a short, friendly reply to the customer that states the item, the
price in USD and whether the request is eligible, and why."""


@workflow.defn
class SupportSupervisorWorkflow:
    """Support Supervisor agent: answers one customer ticket."""

    def __init__(self) -> None:
        self.supervisor = TemporalAgent(
            model=MODEL_KEY,
            system_prompt=SUPERVISOR_PROMPT,
            tools=[
                activity_as_tool(lookup_order, start_to_close_timeout=TOOL_START_TO_CLOSE),
                activity_as_tool(get_return_policy, start_to_close_timeout=TOOL_START_TO_CLOSE),
            ],
            start_to_close_timeout=MODEL_START_TO_CLOSE,
            heartbeat_timeout=MODEL_HEARTBEAT,
            retry_policy=MODEL_RETRY,
            # Shown on each invoke_model Activity in the Web UI (attribution).
            summary="Support Supervisor",
            # Strands prints streamed text to stdout by default: not in a Workflow.
            callback_handler=None,
        )

    @workflow.run
    async def run(self, ticket: str) -> str:
        workflow.logger.info("Support ticket received: %s", ticket)
        result = await self.supervisor.invoke_async(f"Customer ticket: {ticket}")
        return str(result).strip()

    @workflow.query
    def transcript(self) -> list[dict[str, Any]]:
        """The agent's conversation so far (user, assistant and tool messages)."""
        return [dict(message) for message in self.supervisor.messages]


@workflow.defn
class HelloWorkflow:
    """Trivial Workflow that proves the Worker, namespace and test harness work (v0.1)."""

    @workflow.run
    async def run(self, name: str) -> str:
        return await workflow.execute_activity(
            say_hello, name, start_to_close_timeout=timedelta(seconds=10)
        )
