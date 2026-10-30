"""The prompt-drift guard's reading of the Return Policy agent's verdict (unit)."""

from __future__ import annotations

import pytest

from support_agent.workflows import is_eligible

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "answer",
    [
        "ELIGIBLE: damaged on arrival, reported within 30 days (rule 1).",
        "ELIGIBLE. FINANCE_MANAGER_APPROVAL_REQUIRED",
        "Verdict: ELIGIBLE (rule 1)",
    ],
)
def test_eligible_answers(answer: str) -> None:
    assert is_eligible(answer)


@pytest.mark.parametrize(
    "answer",
    [
        "NOT_ELIGIBLE: reported more than 30 days after delivery (rule 3).",
        "NOT ELIGIBLE: reported more than 30 days after delivery.",
        "NOT-ELIGIBLE: outside the window.",
        "INELIGIBLE: outside the window.",
        "eligible",  # the verdict word is upper case by instruction; prose is not a verdict
        "Order 9999 does not exist.",
    ],
)
def test_not_eligible_answers(answer: str) -> None:
    assert not is_eligible(answer)
