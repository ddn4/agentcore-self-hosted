"""The Finance Refund agent's Agent Card, and its markdown rendering.

The card is published twice, and both copies come from this one object:

- as the result of the sync Nexus operation ``FinanceAgentA2A.get_agent_card``
  (A2A discovery at run time), and
- as the **Nexus endpoint description** (``scripts/render-agent-card.py`` ->
  ``.generated/finance-agent-card.md`` -> ``temporal operator nexus endpoint
  create --description-file``), so the A2A peer is discoverable in the Web UI's
  Nexus registry (runbook 7b.8).

``tests/test_agent_card.py`` keeps the markdown in sync with this object
through a golden file.
"""

from __future__ import annotations

from typing import Final

from finance_a2a_api.a2a import AgentCard, AgentSkill

#: Nexus endpoint name in the Temporal endpoint registry (ADR-0004).
ENDPOINT: Final[str] = "finance-agent"
#: Nexus service name: ``get_agent_card`` (sync) and ``send_message`` (workflow-backed).
SERVICE: Final[str] = "FinanceAgentA2A"
#: Refunds up to this amount (inclusive) may be issued without a human approver.
AUTO_APPROVE_LIMIT_USD: Final[float] = 100.0

FINANCE_AGENT_CARD: Final[AgentCard] = AgentCard(
    name="Finance Refund Agent",
    version="1.0.0",
    description=(
        "Owned by Finance. Assesses refund requests (fraud risk, refund history, "
        "approval limits) and issues refunds through the payments system. Refunds "
        f"above ${AUTO_APPROVE_LIMIT_USD:.0f}, or at HIGH fraud risk, need a human "
        "Finance approver."
    ),
    skills=[
        AgentSkill(
            id="refund",
            name="Refund request",
            description=(
                "Approve and issue a customer refund. Send a text part (why) and a "
                "data part {order_id, amount_usd, reason}. The task completes with a "
                "refund_confirmation artifact {refund_id, amount_usd, approved_by}."
            ),
            tags=["refund", "payments", "finance"],
        )
    ],
    url=f"temporal-nexus://{ENDPOINT}/{SERVICE}",
)


def render_markdown(card: AgentCard = FINANCE_AGENT_CARD) -> str:
    """The card as the Nexus endpoint description (runbook 7b.8 layout)."""
    lines = [
        f"# {card.name} (A2A) · v{card.version}",
        "",
        f"**Owner:** Finance · **Runs on:** EKS (`agent-services`) · "
        f"**A2A-shaped:** protocol {card.protocol_version}",
        "",
        f"**Service:** `{SERVICE}` — `get_agent_card` (sync), `send_message` (A2A task, "
        "Workflow `a2a-task-<context_id>`)",
        "",
        card.description,
        "",
    ]
    for skill in card.skills:
        lines.append(f"**Skill `{skill.id}` ({skill.name}):** {skill.description}")
        if skill.tags:
            lines.append(f"Tags: {', '.join(skill.tags)}")
        lines.append("")
    lines.append(
        "Task states: `submitted` → `working` → `completed` | `rejected` | `failed` "
        "(`input-required` while a Finance approver decides)."
    )
    return "\n".join(lines) + "\n"
