"""Determinism: today's Workflow code replays every recorded history.

``tests/histories/*.json`` are real runs recorded with ``make record-histories``
(live Opus 5.5 on the dev server; identities scrubbed). Replaying them needs the
same plugin *shape* the Worker had (sandbox passthroughs, pydantic converter);
the model itself is never called on replay, so a ScriptedModel with an empty
script stands in for Opus 5.5. Idiom: temporal-developer testing.md "Workflow
Replay Testing" and the Python SDK docs (``Replayer.replay_workflow`` with
``WorkflowHistory.from_json``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from demo_testing.history import activity_runs, activity_types
from demo_testing.scripted_model import ScriptedModel, scripted_plugin
from support_agent.worker_factory import WORKFLOWS

pytestmark = pytest.mark.workflow

HISTORIES = sorted((Path(__file__).parent / "histories").glob("*.json"))
# Loaded at collection time (sync): no blocking file I/O inside the async tests.
RECORDED = {path.stem: json.loads(path.read_text()) for path in HISTORIES}


def test_at_least_one_recorded_history() -> None:
    assert HISTORIES, "record one with: make record-histories WF=support-1042"


@pytest.mark.parametrize("workflow_id", sorted(RECORDED))
async def test_recorded_history_replays(workflow_id: str) -> None:
    history = WorkflowHistory.from_json(workflow_id, RECORDED[workflow_id])
    assert "invoke_model" in activity_types(history)
    replayer = Replayer(workflows=WORKFLOWS, plugins=[scripted_plugin(ScriptedModel([]))])
    await replayer.replay_workflow(history)


def test_crash_fixture_shows_only_the_interrupted_turn_retried() -> None:
    """support-crash-scripted: a real Worker process was kill -9'd mid-invoke_model.

    Recorded by the v0.2 verify script's offline crash demo (scripted model, real
    dev server, real processes). Only the interrupted model turn has attempt 2.
    """
    history = WorkflowHistory.from_json(
        "support-crash-scripted", RECORDED["support-crash-scripted"]
    )
    runs = activity_runs(history)
    assert [(r.activity_type, r.attempts) for r in runs] == [
        ("invoke_model", 1),
        ("lookup_order", 1),
        ("invoke_model", 2),
        ("get_return_policy", 1),
        ("invoke_model", 1),
    ]
    assert all(r.completed for r in runs)
