"""A2ASmokeWorkflow (the no-LLM A2A caller): sandbox import and replay of a dev-server run.

The live hop (both namespaces, the endpoint, Finance's Worker) is covered by
``tests/test_a2a_nexus.py``. Here: the fixture recorded by
``RECORD_HISTORY=1 scripts/verify/v0.4-finance-agent.sh`` replays on this code,
with the smoke Worker's own setup (no StrandsPlugin, default converter), and its
history shows the two A2A operations on endpoint ``finance-agent``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from demo_testing.history import nexus_operations
from support_agent.smoke import A2ASmokeWorkflow

pytestmark = pytest.mark.workflow

HISTORIES = sorted((Path(__file__).parent / "histories" / "smoke").glob("*.json"))
RECORDED = {p.stem: json.loads(p.read_text()) for p in HISTORIES}


def test_smoke_fixture_exists() -> None:
    assert RECORDED, "record with: RECORD_HISTORY=1 scripts/verify/v0.4-finance-agent.sh"


@pytest.mark.parametrize("workflow_id", sorted(RECORDED))
async def test_smoke_history_replays_and_shows_the_hop(workflow_id: str) -> None:
    history = WorkflowHistory.from_json(workflow_id, RECORDED[workflow_id])
    await Replayer(workflows=[A2ASmokeWorkflow]).replay_workflow(history)
    ops = nexus_operations(history)
    assert [(o.endpoint, o.service, o.operation, o.outcome) for o in ops] == [
        ("finance-agent", "FinanceAgentA2A", "get_agent_card", "completed"),
        ("finance-agent", "FinanceAgentA2A", "send_message", "completed"),
    ]
    assert ops[1].links == (f"agent-services/a2a-task-{workflow_id}",)
    assert all(o.summary.startswith("A2A → Finance Refund Agent") for o in ops)
