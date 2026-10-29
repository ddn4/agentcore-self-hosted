"""Contract compatibility gate: fails CI on a removed field or a newly required field.

``contract.schema.json`` is a snapshot of every contract dataclass: each field's
type (as written) and whether it is required (has no default). The rules are the
usual ones for a published wire contract with independent deployers (Support on
AgentCore, Finance on EKS):

- a field in the snapshot must still exist, with the same type;
- a field that was optional must not become required;
- a new field must be optional (have a default).

After an allowed change (a new optional field), refresh the snapshot with
``UPDATE_CONTRACT_SNAPSHOT=1 uv run pytest packages/finance-a2a-api`` and commit it.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from finance_a2a_api import a2a

pytestmark = pytest.mark.unit

SNAPSHOT = Path(__file__).with_name("contract.schema.json")
TYPES = [
    a2a.Part,
    a2a.A2AMessage,
    a2a.TaskStatus,
    a2a.Artifact,
    a2a.A2ATask,
    a2a.AgentSkill,
    a2a.AgentCard,
    a2a.AgentCardRequest,
]


def type_name(t: Any) -> str:
    """A stable, readable name for a field type (``list[Part]``, ``str | None``)."""
    if isinstance(t, type) and not hasattr(t, "__args__"):
        return t.__name__
    return re.sub(r"\b(?:finance_a2a_api\.a2a|typing)\.", "", str(t))


def current_schema() -> dict[str, Any]:
    schema: dict[str, Any] = {
        "a2a_protocol_version": a2a.A2A_PROTOCOL_VERSION,
        "task_states": sorted(a2a.TaskState.__args__),  # type: ignore[attr-defined]
        "types": {},
    }
    for cls in TYPES:
        fields = {}
        for f in dataclasses.fields(cls):
            required = f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
            fields[f.name] = {"type": type_name(f.type), "required": required}
        schema["types"][cls.__name__] = fields
    return schema


def breaking_changes(before: dict[str, Any], now: dict[str, Any]) -> list[str]:
    """Every way ``now`` breaks a peer built against ``before``."""
    problems: list[str] = []
    for state in before["task_states"]:
        if state not in now["task_states"]:
            problems.append(f"task state {state!r} removed")
    for type_name, fields in before["types"].items():
        if type_name not in now["types"]:
            problems.append(f"type {type_name} removed")
            continue
        for name, spec in fields.items():
            cur = now["types"][type_name].get(name)
            if cur is None:
                problems.append(f"{type_name}.{name} removed")
            elif cur["type"] != spec["type"]:
                problems.append(f"{type_name}.{name} type {spec['type']} -> {cur['type']}")
            elif cur["required"] and not spec["required"]:
                problems.append(f"{type_name}.{name} became required")
    for type_name, fields in now["types"].items():
        old = before["types"].get(type_name, {})
        for name, spec in fields.items():
            if name not in old and spec["required"]:
                problems.append(f"new field {type_name}.{name} is required (give it a default)")
    return problems


def test_contract_is_backward_compatible() -> None:
    now = current_schema()
    if os.environ.get("UPDATE_CONTRACT_SNAPSHOT") == "1":
        SNAPSHOT.write_text(json.dumps(now, indent=2, sort_keys=True) + "\n")
    problems = breaking_changes(json.loads(SNAPSHOT.read_text()), now)
    assert not problems, "breaking contract change:\n  " + "\n  ".join(problems)


def test_snapshot_is_current() -> None:
    """Additive changes are allowed, but the committed snapshot must record them."""
    assert json.loads(SNAPSHOT.read_text()) == current_schema(), (
        "contract changed compatibly; refresh with UPDATE_CONTRACT_SNAPSHOT=1"
    )


def test_gate_catches_breaking_changes() -> None:
    """The gate itself, on hand-broken copies of the current schema."""
    before = current_schema()
    now = json.loads(json.dumps(before))
    del now["types"]["A2ATask"]["artifacts"]
    now["types"]["A2AMessage"]["priority"] = {"type": "int", "required": True}
    now["types"]["A2AMessage"]["task_id"]["required"] = True
    now["types"]["Part"]["text"]["type"] = "str"
    now["task_states"].remove("rejected")
    assert sorted(breaking_changes(before, now)) == sorted(
        [
            "A2ATask.artifacts removed",
            "new field A2AMessage.priority is required (give it a default)",
            "A2AMessage.task_id became required",
            "Part.text type str | None -> str",
            "task state 'rejected' removed",
        ]
    )
    # Adding an optional field is fine.
    now = json.loads(json.dumps(before))
    now["types"]["A2ATask"]["history"] = {"type": "list[A2AMessage]", "required": False}
    assert breaking_changes(before, now) == []
