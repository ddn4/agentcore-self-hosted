"""Determinism: today's Workflow code replays every recorded history.

``tests/histories/*.json`` are real dev-server runs of the current Workflow code,
recorded with ``make record-histories`` (identities scrubbed): the
``*-scripted`` ones by ``scripts/verify/v0.3-sub-agents.sh`` with the scripted
model (real Worker processes, a real ``kill -9``), ``support-1042`` /
``support-2077`` from live Opus 5.5 runs when AWS is available. Replaying needs
the same plugin *shape* the Worker had (sandbox passthroughs, pydantic
converter); the model itself is never called on replay, so a ScriptedModel with
an empty script stands in for Opus 5.5. Idiom: temporal-developer testing.md
"Workflow Replay Testing" and the Python SDK docs (``Replayer.replay_workflow``
with ``WorkflowHistory.from_json``).

``tests/histories/v0.2/`` keeps the v0.2 recording (Supervisor with direct
Activity tools). v0.3 changed the command sequence, so it must **not** replay:
that test documents the breaking change (open v0.2 runs had to finish before
the v0.3 Worker took ``support-dev-tq``; from v0.7, Worker Versioning pins each
run to the code that started it).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer
from temporalio.workflow import NondeterminismError

from demo_testing.history import activity_runs, activity_types, attributed_activities
from demo_testing.scripted_model import ScriptedModel, scripted_plugin
from support_agent.worker_factory import WORKFLOWS

pytestmark = pytest.mark.workflow

HERE = Path(__file__).parent / "histories"
HISTORIES = sorted(HERE.glob("*.json"))
V02_HISTORIES = sorted((HERE / "v0.2").glob("*.json"))
# Loaded at collection time (sync): no blocking file I/O inside the async tests.
RECORDED = {path.stem: json.loads(path.read_text()) for path in HISTORIES}
RECORDED_V02 = {path.stem: json.loads(path.read_text()) for path in V02_HISTORIES}


def replayer() -> Replayer:
    return Replayer(workflows=WORKFLOWS, plugins=[scripted_plugin(ScriptedModel([]))])


def test_at_least_one_recorded_history() -> None:
    assert HISTORIES, "record one with: make record-histories WF=support-1042"


@pytest.mark.parametrize("workflow_id", sorted(RECORDED))
async def test_recorded_history_replays(workflow_id: str) -> None:
    history = WorkflowHistory.from_json(workflow_id, RECORDED[workflow_id])
    assert "invoke_model" in activity_types(history)
    await replayer().replay_workflow(history)


@pytest.mark.parametrize("workflow_id", sorted(RECORDED))
def test_recorded_history_shows_the_sub_agents(workflow_id: str) -> None:
    """v0.3 acceptance: separate, attributed invoke_model sequences per agent."""
    history = WorkflowHistory.from_json(workflow_id, RECORDED[workflow_id])
    attributed = set(attributed_activities(history))
    for agent in ("Support Supervisor", "Order Lookup agent", "Return Policy agent"):
        assert f"invoke_model [{agent}]" in attributed


@pytest.mark.parametrize("workflow_id", sorted(RECORDED_V02))
async def test_v02_history_does_not_replay_on_v03_code(workflow_id: str) -> None:
    history = WorkflowHistory.from_json(workflow_id, RECORDED_V02[workflow_id])
    result = await replayer().replay_workflow(history, raise_on_replay_failure=False)
    assert isinstance(result.replay_failure, NondeterminismError), result.replay_failure


def test_crash_fixture_shows_only_the_interrupted_sub_agent_turn_retried() -> None:
    """support-crash-scripted: a real Worker process was kill -9'd mid-invoke_model.

    Recorded by the v0.3 verify script's offline crash demo (scripted model, real
    dev server, real processes). The kill landed in the Order Lookup agent's
    second model turn (after ``lookup_order`` completed), two agent loops deep;
    only that turn has attempt 2.
    """
    history = WorkflowHistory.from_json(
        "support-crash-scripted", RECORDED["support-crash-scripted"]
    )
    runs = activity_runs(history)
    assert [(r.activity_type, r.summary, r.attempts) for r in runs] == [
        ("invoke_model", "Support Supervisor", 1),
        ("invoke_model", "Order Lookup agent", 1),
        ("lookup_order", "", 1),
        ("invoke_model", "Order Lookup agent", 2),
        ("invoke_model", "Support Supervisor", 1),
        ("invoke_model", "Return Policy agent", 1),
        ("get_return_policy", "", 1),
        ("invoke_model", "Return Policy agent", 1),
        ("invoke_model", "Support Supervisor", 1),
    ]
    assert all(r.completed for r in runs)
