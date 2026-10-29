"""Team Finance's agent: the Finance Refund agent (a Strands ``TemporalAgent``).

Its model calls are the plugin's ``invoke_model`` Activities and its tools are
the ``fraud_score`` and ``refund_history`` Activities (``activity_as_tool``), so
every step is recorded and retried by Temporal (samples-python
``strands_plugin/tools/workflow.py``; same model-Activity options as Support's
agents, ADR-0001). The agent only *recommends* (``RISK=LOW|HIGH`` plus a
justification for the approver); ``policy.decide`` makes the decision.

Workflow code: imported by ``workflows.py`` in the sandbox; Activities and
Strands are passed through (samples-python ``bedrock_agentcore/strands_agent``).
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.strands_agents import TemporalAgent
from temporalio.strands_agents.workflow import activity_as_tool

with workflow.unsafe.imports_passed_through():
    from finance_agent.activities import fraud_score, refund_history

#: Model key pinned by ``config.build_strands_plugin()`` (ADR-0001).
MODEL_KEY = "bedrock"
MODEL_START_TO_CLOSE = timedelta(seconds=120)
#: The plugin heartbeats while the model streams: a dead Worker is noticed in ~10 s.
MODEL_HEARTBEAT = timedelta(seconds=10)
#: Temporal owns retries (botocore retries are off); bounded so an HTTP 400 fails.
MODEL_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=20),
    maximum_attempts=5,
)
TOOL_START_TO_CLOSE = timedelta(seconds=10)

#: ``summary`` on the agent's ``invoke_model`` Activities (Web UI attribution).
FINANCE_AGENT_NAME = "Finance Refund agent"

FINANCE_PROMPT = """You are the Finance team's Refund agent. Another team's agent
(Customer Support) asks you to refund a customer order.
Assess the request: call fraud_score and refund_history for the order, then
answer with RISK=LOW or RISK=HIGH on the first line, followed by a one-paragraph
justification a Finance approver can read. Use RISK=HIGH when fraud_score says
HIGH or the refund history looks abusive. You do not issue refunds and you do not
decide approval limits: Finance's code does that."""


def new_finance_agent() -> TemporalAgent:
    """The Finance Refund agent on the pinned model (fresh conversation)."""
    return TemporalAgent(
        model=MODEL_KEY,
        name=FINANCE_AGENT_NAME,
        system_prompt=FINANCE_PROMPT,
        tools=[
            activity_as_tool(fraud_score, start_to_close_timeout=TOOL_START_TO_CLOSE),
            activity_as_tool(refund_history, start_to_close_timeout=TOOL_START_TO_CLOSE),
        ],
        start_to_close_timeout=MODEL_START_TO_CLOSE,
        heartbeat_timeout=MODEL_HEARTBEAT,
        retry_policy=MODEL_RETRY,
        summary=FINANCE_AGENT_NAME,
        # Strands prints streamed text to stdout by default: not in a Workflow.
        callback_handler=None,
    )
