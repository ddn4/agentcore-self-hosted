"""A2A-shaped types and the ``FinanceAgentA2A`` Nexus service definition.

The payloads are *shaped like* the A2A protocol (Agent Card, Message/Part,
Task/TaskState, Artifact) so viewers recognise them; the transport is Nexus,
which makes the agent-to-agent call durable (runbook §1.4, 5.6.1).

**Spec pinned: A2A 0.3.0** (see ``README.md`` for the field mapping and the
deltas to A2A 1.0.0). Field names are the spec's concepts in Python
``snake_case`` (``contextId`` -> ``context_id``); task states and part kinds
use the 0.3.0 string values (``input-required``, ``kind: "text"``). This is
**A2A-shaped, not A2A wire-compatible**: a thin JSON-RPC gateway would map the
names if a non-Temporal A2A client ever needs to call Finance.

The service definition follows samples-python ``hello_nexus/service.py``: plain
dataclasses for input/output types and a ``@nexusrpc.service`` class whose
attributes are ``nexusrpc.Operation[Input, Output]``. Callers use it to build a
type-safe Nexus client (``workflow.create_nexus_client``); the handler uses it
to check that it implements every operation with the right types.

Compatibility rules (enforced by ``tests/test_schema_snapshot.py``): never
remove or rename a field, and every field added later must have a default.

**No ``from __future__ import annotations`` in this module.** The SDK's default
converter resolves dataclass field types with ``typing.get_type_hints``; inside
a caller's Workflow sandbox, string annotations such as ``"list[AgentSkill]"``
failed to resolve (``NameError: name 'AgentSkill' is not defined``) and the
caller's Workflow Task failed while decoding the Nexus result (found by
``tests/test_a2a_nexus.py`` at v0.4). Real annotations avoid the lookup; every
type is defined before it is used.
"""

from dataclasses import dataclass, field
from typing import Any, Final, Literal

import nexusrpc

#: The A2A protocol version these shapes follow (pinned in README.md).
A2A_PROTOCOL_VERSION: Final[str] = "0.3.0"

TaskState = Literal[
    "submitted",
    "working",
    "input-required",
    "completed",
    "rejected",
    "failed",
    "canceled",
]
#: States after which the task never changes again (the Nexus operation result).
TERMINAL_STATES: Final[frozenset[str]] = frozenset({"completed", "rejected", "failed", "canceled"})

Role = Literal["user", "agent"]
PartKind = Literal["text", "data"]


@dataclass
class Part:
    """A2A ``TextPart`` (``kind="text"``) or ``DataPart`` (``kind="data"``)."""

    kind: PartKind
    text: str | None = None
    data: dict[str, Any] | None = None


@dataclass
class A2AMessage:
    """A2A ``Message``: one turn from the caller (``user``) or the agent (``agent``)."""

    role: Role
    parts: list[Part]
    message_id: str
    #: Correlates the conversation. Support sets it to its own Workflow ID.
    context_id: str
    task_id: str | None = None


@dataclass
class TaskStatus:
    """A2A ``TaskStatus``: the current state plus the agent's latest message."""

    state: TaskState
    message: A2AMessage | None = None
    #: ISO 8601 time of the change (Workflow time on the Finance side).
    timestamp: str | None = None


@dataclass
class Artifact:
    """A2A ``Artifact``: an output of the task (Finance: ``refund_confirmation``)."""

    artifact_id: str
    name: str
    parts: list[Part]


@dataclass
class A2ATask:
    """A2A ``Task``. Finance's ID is its Workflow ID, ``a2a-task-<context_id>``."""

    id: str
    context_id: str
    status: TaskStatus
    artifacts: list[Artifact] = field(default_factory=lambda: list[Artifact]())


@dataclass
class AgentSkill:
    """A2A ``AgentSkill``."""

    id: str
    name: str
    description: str
    tags: list[str] = field(default_factory=lambda: list[str]())


@dataclass
class AgentCard:
    """A2A ``AgentCard`` (the discovery document), returned by ``get_agent_card``."""

    name: str
    description: str
    version: str
    skills: list[AgentSkill]
    #: Where the agent is reached. Not an HTTP URL: a Nexus endpoint and service.
    url: str = ""
    protocol_version: str = A2A_PROTOCOL_VERSION
    default_input_modes: list[str] = field(default_factory=lambda: ["text", "data"])
    default_output_modes: list[str] = field(default_factory=lambda: ["text", "data"])


@dataclass
class AgentCardRequest:
    """Input of ``get_agent_card`` (empty: ``--input '{}'`` from the CLI)."""


@nexusrpc.service
class FinanceAgentA2A:
    """The Finance Refund agent's published A2A-over-Nexus contract.

    - ``get_agent_card``: **sync** operation (must answer within 10 s): discovery.
    - ``send_message``: **async**, workflow-backed: starts (or joins) the A2A task
      ``a2a-task-<context_id>`` and completes with the terminal ``A2ATask``.
    """

    get_agent_card: nexusrpc.Operation[AgentCardRequest, AgentCard]
    send_message: nexusrpc.Operation[A2AMessage, A2ATask]


# --- helpers both teams may use (pure functions over the types above) ----------


def text_part(text: str) -> Part:
    return Part(kind="text", text=text)


def data_part(data: dict[str, Any]) -> Part:
    return Part(kind="data", data=data)


def first_text(parts: list[Part]) -> str:
    """Text of the first text part, or ``""``."""
    return next((p.text for p in parts if p.kind == "text" and p.text is not None), "")


def first_data(parts: list[Part]) -> dict[str, Any] | None:
    """Data of the first data part, or ``None``."""
    return next((p.data for p in parts if p.kind == "data" and p.data is not None), None)


def status_text(task: A2ATask) -> str:
    """The agent's latest message text on a task, or ``""``."""
    return first_text(task.status.message.parts) if task.status.message else ""


def refund_request(
    *,
    message_id: str,
    context_id: str,
    order_id: str,
    amount_usd: float,
    reason: str,
    summary: str,
) -> A2AMessage:
    """The message Support sends Finance to ask for a refund (runbook 5.6, 7b.9)."""
    return A2AMessage(
        role="user",
        message_id=message_id,
        context_id=context_id,
        parts=[
            text_part(summary),
            data_part({"order_id": order_id, "amount_usd": amount_usd, "reason": reason}),
        ],
    )
