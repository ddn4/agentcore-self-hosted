"""ADR-0009: the caller's Nexus budget is above Finance's approval wait (plan v0.6).

A unit test across both teams' configuration (it lives at the repo root, like the
cross-namespace tests, because neither service may import the other):

- the defaults: Support 25 h >= Finance 24 h + 1 h, both derived from the limit
  Finance publishes in its Agent Card (``finance_a2a_api.APPROVAL_WAIT_LIMIT``);
- the committed ``.env.example`` values;
- Finance refuses a configured wait above the published limit, so no
  ``FINANCE_APPROVAL_TIMEOUT`` can outgrow a caller budgeted from the card.

Why the order matters (Support's ``test_workflow.py``,
``test_caller_budget_shorter_than_finance_wait_is_a_nexus_timeout``): if the
caller's budget ran out first, Support would get a ``NexusOperationError``
(timeout) with no answer from Finance, and the Finance task would carry on
waiting for an approver.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from finance_a2a_api import APPROVAL_WAIT_LIMIT
from finance_agent import config as finance_config
from finance_agent.durations import parse_duration
from support_agent import config as support_config

pytestmark = pytest.mark.unit

HEADROOM = timedelta(hours=1)
ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


def env_example() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FINANCE_APPROVAL_TIMEOUT", raising=False)
    monkeypatch.delenv("SUPPORT_NEXUS_SEND_TIMEOUT", raising=False)


def test_defaults_keep_the_caller_budget_an_hour_above_the_approval_wait() -> None:
    finance = parse_duration(finance_config.approval_timeout())
    support = parse_duration(support_config.nexus_send_timeout())
    assert finance == APPROVAL_WAIT_LIMIT == timedelta(hours=24)
    assert support >= finance + HEADROOM
    assert support == timedelta(hours=25)


def test_env_example_keeps_the_invariant(monkeypatch: pytest.MonkeyPatch) -> None:
    values = env_example()
    monkeypatch.setenv("FINANCE_APPROVAL_TIMEOUT", values["FINANCE_APPROVAL_TIMEOUT"])
    monkeypatch.setenv("SUPPORT_NEXUS_SEND_TIMEOUT", values["SUPPORT_NEXUS_SEND_TIMEOUT"])
    finance = parse_duration(finance_config.approval_timeout())
    support = parse_duration(support_config.nexus_send_timeout())
    assert support >= finance + HEADROOM


def test_a_caller_budgeted_from_the_card_always_outlasts_finance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whatever Finance configures is at most the card's limit, so 25 h always covers it."""
    support = parse_duration(support_config.nexus_send_timeout())
    for value in ("5s", "30m", "12h", "24h"):
        monkeypatch.setenv("FINANCE_APPROVAL_TIMEOUT", value)
        assert support >= parse_duration(finance_config.approval_timeout()) + HEADROOM
    monkeypatch.setenv("FINANCE_APPROVAL_TIMEOUT", "24h1s")
    with pytest.raises(ValueError, match="published approval limit"):
        finance_config.approval_timeout()
