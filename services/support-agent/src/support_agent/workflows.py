"""Workflows for team Support (caller), namespace ``agents``.

``SupportSupervisorWorkflow`` runs the Support Supervisor, a Strands agent whose
model calls are Temporal Activities (the plugin's ``invoke_model``).

- v0.2: the Supervisor called the ``lookup_order`` and ``get_return_policy``
  Activities directly (``activity_as_tool``).
- v0.3 (this version): the Supervisor's tools are two Support-owned
  **sub-agents**, the Order Lookup agent and the Return Policy agent
  (``support_agent.agents``). Each is a ``TemporalAgent`` called from an
  in-workflow Strands ``@tool``: a nested agent loop inside this one Workflow
  (Q20). The history shows the Supervisor's ``invoke_model`` turns and, between
  them, each sub-agent's own ``invoke_model`` turns and tool Activity,
  labelled by the ``summary`` on every model Activity.

If the Worker dies mid-turn (the Supervisor's or a sub-agent's), the in-flight
Activity is retried and everything already recorded is replayed from history,
including the nested loops (``test_crash_safety.py``).

**Incompatible with v0.2 histories** (the command sequence changed): open v0.2
runs on an unversioned queue must finish or be terminated before this Worker
takes the queue. From v0.7 Worker Versioning (PINNED) makes such changes safe;
``test_replay.py`` keeps the v0.2 fixture to show the non-determinism error.

Shape follows samples-python ``strands_plugin/tools/workflow.py`` (agent built
in ``__init__``, plain ``@tool`` functions run in Workflow context,
``invoke_async`` in ``run``), ``bedrock_agentcore/strands_agent/workflows.py``
(prompt constants, pass-through Activity imports) and ``strands_plugin/
human_in_the_loop`` (query over Workflow state).
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta
from typing import Any

from temporalio import workflow

from support_agent.agents import SubAgentRun, new_supervisor, sub_agent_tools

with workflow.unsafe.imports_passed_through():
    from support_agent.activities import say_hello


@workflow.defn
class SupportSupervisorWorkflow:
    """Support Supervisor agent plus its sub-agents: answers one customer ticket."""

    def __init__(self) -> None:
        self._sub_agent_runs: list[SubAgentRun] = []
        self.supervisor = new_supervisor(sub_agent_tools(self._sub_agent_runs.append))

    @workflow.run
    async def run(self, ticket: str) -> str:
        workflow.logger.info("Support ticket received: %s", ticket)
        result = await self.supervisor.invoke_async(f"Customer ticket: {ticket}")
        return str(result).strip()

    @workflow.query
    def transcript(self) -> list[dict[str, Any]]:
        """The Supervisor's conversation so far (user, assistant and tool messages)."""
        return [dict(message) for message in self.supervisor.messages]

    @workflow.query
    def sub_agent_runs(self) -> list[dict[str, str]]:
        """Each finished sub-agent call: which agent, the question, its answer."""
        return [asdict(run) for run in self._sub_agent_runs]


@workflow.defn
class HelloWorkflow:
    """Trivial Workflow that proves the Worker, namespace and test harness work (v0.1)."""

    @workflow.run
    async def run(self, name: str) -> str:
        return await workflow.execute_activity(
            say_hello, name, start_to_close_timeout=timedelta(seconds=10)
        )
