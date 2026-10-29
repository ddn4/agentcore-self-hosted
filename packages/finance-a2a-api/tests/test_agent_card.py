"""The Agent Card markdown (the Nexus endpoint description) matches the code.

``finance-agent-card.golden.md`` is what ``make nexus-endpoint`` publishes as the
endpoint description, reviewed in git. If the card changes, regenerate it with
``make agent-card-golden``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from finance_a2a_api import (
    A2A_PROTOCOL_VERSION,
    AUTO_APPROVE_LIMIT_USD,
    ENDPOINT,
    FINANCE_AGENT_CARD,
    SERVICE,
    render_markdown,
)

pytestmark = pytest.mark.unit

GOLDEN = Path(__file__).with_name("finance-agent-card.golden.md")


def test_markdown_matches_golden() -> None:
    assert render_markdown() == GOLDEN.read_text(encoding="utf-8")


def test_markdown_carries_every_card_fact() -> None:
    md = render_markdown()
    card = FINANCE_AGENT_CARD
    for fact in (
        card.name,
        card.version,
        card.description,
        SERVICE,
        "get_agent_card",
        "send_message",
    ):
        assert fact in md
    for skill in card.skills:
        assert skill.id in md and skill.description in md


def test_card_facts() -> None:
    card = FINANCE_AGENT_CARD
    assert card.protocol_version == A2A_PROTOCOL_VERSION
    assert card.url == f"temporal-nexus://{ENDPOINT}/{SERVICE}"
    assert [s.id for s in card.skills] == ["refund"]
    assert f"${AUTO_APPROVE_LIMIT_USD:.0f}" in card.description
