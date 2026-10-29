"""Every contract type survives the wire, whichever converter each side uses.

Support's Workers (from v0.5) and Finance's Worker carry the StrandsPlugin, which
installs the pydantic data converter; the no-LLM smoke caller (v0.4) uses the
SDK's default converter. Nexus payloads cross between them, so every pairing is
checked. Also: runbook 7b.9's hand-written CLI JSON must parse.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from temporalio.api.common.v1 import Payload
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.converter import DataConverter, default

from finance_a2a_api import (
    FINANCE_AGENT_CARD,
    A2AMessage,
    A2ATask,
    AgentCard,
    AgentCardRequest,
    AgentSkill,
    Artifact,
    Part,
    TaskStatus,
    data_part,
    first_data,
    first_text,
    refund_request,
    status_text,
    text_part,
)

pytestmark = pytest.mark.unit

CONVERTERS = {"default": default(), "pydantic": pydantic_data_converter}

MESSAGE = refund_request(
    message_id="m-1",
    context_id="support-1042",
    order_id="1042",
    amount_usd=40.0,
    reason="damaged",
    summary="Customer's headphones arrived broken.",
)
TASK = A2ATask(
    id="a2a-task-support-1042",
    context_id="support-1042",
    status=TaskStatus(
        state="completed",
        message=A2AMessage(
            role="agent",
            parts=[text_part("Refund R-10001 issued for $40.00.")],
            message_id="m-2",
            context_id="support-1042",
            task_id="a2a-task-support-1042",
        ),
        timestamp="2026-10-29T12:00:00+00:00",
    ),
    artifacts=[
        Artifact(
            artifact_id="refund_confirmation-1",
            name="refund_confirmation",
            parts=[data_part({"refund_id": "R-10001", "amount_usd": 40.0, "approved_by": "auto"})],
        )
    ],
)
SAMPLES: list[tuple[type[Any], Any]] = [
    (Part, text_part("hi")),
    (Part, data_part({"a": 1})),
    (A2AMessage, MESSAGE),
    (TaskStatus, TaskStatus(state="working")),
    (TaskStatus, TASK.status),
    (Artifact, TASK.artifacts[0]),
    (A2ATask, TASK),
    (A2ATask, A2ATask(id="t", context_id="c", status=TaskStatus("submitted"))),
    (AgentSkill, FINANCE_AGENT_CARD.skills[0]),
    (AgentCard, FINANCE_AGENT_CARD),
    (AgentCardRequest, AgentCardRequest()),
]


@pytest.mark.parametrize("encoder", CONVERTERS)
@pytest.mark.parametrize("decoder", CONVERTERS)
@pytest.mark.parametrize(("type_", "value"), SAMPLES, ids=lambda v: getattr(v, "__name__", ""))
def test_round_trip(encoder: str, decoder: str, type_: type[Any], value: Any) -> None:
    payloads = CONVERTERS[encoder].payload_converter.to_payloads([value])
    assert payloads[0].metadata["encoding"] == b"json/plain"
    (out,) = CONVERTERS[decoder].payload_converter.from_payloads(payloads, [type_])
    assert out == value


def _payload(data: Any) -> Payload:
    raw = data if isinstance(data, str) else json.dumps(data)
    return Payload(metadata={"encoding": b"json/plain"}, data=raw.encode())


#: Runbook 7b.9, verbatim: the input an operator types for a standalone operation.
CLI_SEND_MESSAGE = (
    '{"role":"user","message_id":"m1","context_id":"smoke-1","parts":'
    '[{"kind":"text","text":"Broken headphones"},'
    '{"kind":"data","data":{"order_id":"1042","amount_usd":40,"reason":"damaged"}}]}'
)


@pytest.mark.parametrize("name", CONVERTERS)
def test_runbook_cli_json_parses(name: str) -> None:
    converter: DataConverter = CONVERTERS[name]
    (msg,) = converter.payload_converter.from_payloads([_payload(CLI_SEND_MESSAGE)], [A2AMessage])
    assert msg.context_id == "smoke-1"
    assert first_text(msg.parts) == "Broken headphones"
    assert first_data(msg.parts) == {"order_id": "1042", "amount_usd": 40, "reason": "damaged"}
    (card_req,) = converter.payload_converter.from_payloads([_payload("{}")], [AgentCardRequest])
    assert card_req == AgentCardRequest()


@pytest.mark.parametrize("name", CONVERTERS)
def test_unknown_fields_are_ignored(name: str) -> None:
    """Forward compatibility: a newer peer may send fields this version lacks."""
    converter = CONVERTERS[name]
    (status,) = converter.payload_converter.from_payloads(
        [_payload({"state": "working", "kind": "status-update"})], [TaskStatus]
    )
    assert status == TaskStatus(state="working")


@pytest.mark.parametrize("name", CONVERTERS)
def test_unknown_task_state_is_rejected(name: str) -> None:
    converter = CONVERTERS[name]
    with pytest.raises(Exception):  # noqa: B017 - TypeError (default) or ValidationError (pydantic)
        converter.payload_converter.from_payloads([_payload({"state": "bogus"})], [TaskStatus])


def test_helpers() -> None:
    assert first_text(MESSAGE.parts) == "Customer's headphones arrived broken."
    assert first_data(MESSAGE.parts) == {
        "order_id": "1042",
        "amount_usd": 40.0,
        "reason": "damaged",
    }
    assert first_text([data_part({})]) == ""
    assert first_data([text_part("x")]) is None
    assert status_text(TASK) == "Refund R-10001 issued for $40.00."
    assert status_text(A2ATask(id="t", context_id="c", status=TaskStatus("working"))) == ""
