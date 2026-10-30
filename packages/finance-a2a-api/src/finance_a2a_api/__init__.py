"""The Finance agent's published A2A-shaped contract.

This is the ONLY code shared between team Support and team Finance (plan §2.2).
It depends only on ``nexus-rpc`` and is type-checked with pyright strict.

- ``finance_a2a_api.a2a``: Message/Part, Task/TaskStatus/TaskState, Artifact,
  AgentCard/AgentSkill dataclasses and the ``FinanceAgentA2A`` Nexus service.
- ``finance_a2a_api.agent_card``: ``FINANCE_AGENT_CARD`` and ``render_markdown()``.
"""

from finance_a2a_api.a2a import (
    A2A_PROTOCOL_VERSION,
    TERMINAL_STATES,
    A2AMessage,
    A2ATask,
    AgentCard,
    AgentCardRequest,
    AgentSkill,
    Artifact,
    FinanceAgentA2A,
    Part,
    TaskState,
    TaskStatus,
    data_part,
    first_data,
    first_text,
    refund_request,
    status_text,
    text_part,
)
from finance_a2a_api.agent_card import (
    APPROVAL_WAIT_LIMIT,
    AUTO_APPROVE_LIMIT_USD,
    ENDPOINT,
    FINANCE_AGENT_CARD,
    SERVICE,
    render_markdown,
)

__all__ = [
    "A2A_PROTOCOL_VERSION",
    "APPROVAL_WAIT_LIMIT",
    "AUTO_APPROVE_LIMIT_USD",
    "ENDPOINT",
    "FINANCE_AGENT_CARD",
    "SERVICE",
    "TERMINAL_STATES",
    "A2AMessage",
    "A2ATask",
    "AgentCard",
    "AgentCardRequest",
    "AgentSkill",
    "Artifact",
    "FinanceAgentA2A",
    "Part",
    "TaskState",
    "TaskStatus",
    "data_part",
    "first_data",
    "first_text",
    "refund_request",
    "render_markdown",
    "status_text",
    "text_part",
]
