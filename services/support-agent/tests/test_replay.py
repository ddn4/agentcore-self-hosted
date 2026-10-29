"""Determinism: today's Workflow code replays every recorded history.

``tests/histories/*.json`` are real dev-server runs of the current Workflow code,
recorded with ``make record-histories`` (identities scrubbed): the
``*-scripted`` ones by ``RECORD_HISTORY=1 scripts/verify/v0.5-a2a-over-nexus.sh``
with the scripted Support team and the scripted Finance agent (real Worker
processes, a real ``kill -9``, the real cross-namespace hop), ``support-1042`` /
``support-2077`` from live Opus 5.5 runs when AWS is available. Replaying needs
the same plugin *shape* the Worker had (sandbox passthroughs, pydantic
converter); the model is never called on replay, and neither is Finance: Nexus
results come from history like Activity results. Idiom: temporal-developer
testing.md "Workflow Replay Testing" and the Python SDK docs
(``Replayer.replay_workflow`` with ``WorkflowHistory.from_json``).

Older shapes are kept to document each breaking change on the unversioned dev
queue (open runs had to finish before the new Worker took ``support-dev-tq``;
from v0.7, Worker Versioning pins each run to the code that started it):

- ``tests/histories/v0.2/``: Supervisor with direct Activity tools.
- ``tests/histories/v0.3/``: Supervisor with sub-agents, no Finance (v0.5 starts
  every run with the ``get_agent_card`` Nexus operation).

Both must raise ``NondeterminismError`` on today's code.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer
from temporalio.workflow import NondeterminismError

from demo_testing.history import activity_runs, activity_types, nexus_operations, timeline
from demo_testing.scripted_model import ScriptedModel, scripted_plugin
from support_agent.worker_factory import WORKFLOWS

pytestmark = pytest.mark.workflow

HERE = Path(__file__).parent / "histories"
HISTORIES = sorted(HERE.glob("*.json"))
OLD_HISTORIES = sorted(HERE.glob("v0.[23]/*.json"))
# Loaded at collection time (sync): no blocking file I/O inside the async tests.
RECORDED = {path.stem: json.loads(path.read_text()) for path in HISTORIES}
RECORDED_OLD = {f"{p.parent.name}/{p.stem}": json.loads(p.read_text()) for p in OLD_HISTORIES}
CARD = "nexus FinanceAgentA2A.get_agent_card"
SEND = "nexus FinanceAgentA2A.send_message"


def replayer() -> Replayer:
    return Replayer(workflows=WORKFLOWS, plugins=[scripted_plugin(ScriptedModel([]))])


def test_at_least_one_recorded_history() -> None:
    assert HISTORIES, "record with: RECORD_HISTORY=1 scripts/verify/v0.5-a2a-over-nexus.sh"


@pytest.mark.parametrize("workflow_id", sorted(RECORDED))
async def test_recorded_history_replays(workflow_id: str) -> None:
    history = WorkflowHistory.from_json(workflow_id, RECORDED[workflow_id])
    assert "invoke_model" in activity_types(history)
    await replayer().replay_workflow(history)


@pytest.mark.parametrize("workflow_id", sorted(RECORDED))
def test_recorded_history_shows_the_team_and_the_a2a_hop(workflow_id: str) -> None:
    """v0.3: attributed sub-agent turns. v0.5: discovery first, then the A2A hop."""
    history = WorkflowHistory.from_json(workflow_id, RECORDED[workflow_id])
    steps = timeline(history)
    for agent in ("Support Supervisor", "Order Lookup agent", "Return Policy agent"):
        assert f"invoke_model [{agent}]" in steps
    assert steps[0] == CARD
    assert SEND in steps
    # send_message comes after the Return Policy agent's decision and before
    # the Supervisor's last turn.
    send = steps.index(SEND)
    assert max(i for i, s in enumerate(steps) if "Return Policy agent" in s) < send
    assert steps[-1] == "invoke_model [Support Supervisor]"
    ops = nexus_operations(history)
    assert {o.operation: o.outcome for o in ops} == {
        "get_agent_card": "completed",
        "send_message": "completed",
    }
    (send_op,) = [o for o in ops if o.operation == "send_message"]
    assert send_op.links and send_op.links[0].startswith("agent-services/a2a-task-")


@pytest.mark.parametrize("name", sorted(RECORDED_OLD))
async def test_older_histories_do_not_replay_on_v05_code(name: str) -> None:
    history = WorkflowHistory.from_json(name, RECORDED_OLD[name])
    result = await replayer().replay_workflow(history, raise_on_replay_failure=False)
    assert isinstance(result.replay_failure, NondeterminismError), result.replay_failure


def test_crash_fixture_shows_only_the_interrupted_turn_retried() -> None:
    """support-crash-scripted: a real Worker process was kill -9'd mid-invoke_model.

    Recorded by the v0.5 verify script's offline crash demo (scripted model, real
    dev server, real processes, real Finance across namespaces). Exactly one
    model turn has attempt 2; every tool Activity ran once; the A2A hop happened
    once.
    """
    history = WorkflowHistory.from_json(
        "support-crash-scripted", RECORDED["support-crash-scripted"]
    )
    runs = activity_runs(history)
    retried = [r for r in runs if r.attempts > 1]
    assert [(r.activity_type, r.attempts) for r in retried] == [("invoke_model", 2)]
    assert all(r.attempts == 1 for r in runs if r.activity_type != "invoke_model")
    assert all(r.completed for r in runs)
    assert timeline(history).count(SEND) == 1
