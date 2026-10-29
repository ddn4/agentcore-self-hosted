"""The approval rule is code, not the model (runbook 7b.4)."""

import pytest

from finance_agent.policy import RefundRequest, decide, parse_refund_request, parse_risk

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("text", "risk"),
    [
        ("RISK=LOW\nAll good.", "LOW"),
        ("risk = low because...", "LOW"),
        ("RISK=HIGH\nthree refunds", "HIGH"),
        ("Looks fine to me.", "HIGH"),  # no verdict: fail safe
        ("RISK=LOW ... on reflection RISK=HIGH", "HIGH"),  # ambiguous: fail safe
        ("RISK=LOWISH", "HIGH"),
    ],
)
def test_parse_risk(text: str, risk: str) -> None:
    assert parse_risk(text) == risk


@pytest.mark.parametrize(
    ("amount", "risk", "decision"),
    [
        (40.0, "LOW", "auto_approve"),
        (100.0, "LOW", "auto_approve"),  # the limit is inclusive
        (100.01, "LOW", "needs_approver"),
        (450.0, "LOW", "needs_approver"),
        (40.0, "HIGH", "needs_approver"),
    ],
)
def test_decide(amount: float, risk: str, decision: str) -> None:
    assert decide(amount, risk) == decision  # type: ignore[arg-type]


def test_parse_refund_request() -> None:
    assert parse_refund_request({"order_id": "#1042", "amount_usd": 40, "reason": "damaged"}) == (
        RefundRequest("1042", 40.0, "damaged")
    )
    assert parse_refund_request({"order_id": "1042", "amount_usd": "39.999"}) == RefundRequest(
        "1042", 40.0, ""
    )


@pytest.mark.parametrize(
    ("data", "problem"),
    [
        (None, "no data part"),
        ({}, "no data part"),
        ({"amount_usd": 40}, "order_id is missing"),
        ({"order_id": "1042"}, "not a number"),
        ({"order_id": "1042", "amount_usd": "lots"}, "not a number"),
        ({"order_id": "1042", "amount_usd": 0}, "must be positive"),
        ({"order_id": "1042", "amount_usd": float("nan")}, "must be positive"),
    ],
)
def test_invalid_requests(data: dict[str, object] | None, problem: str) -> None:
    result = parse_refund_request(data)
    assert isinstance(result, str) and problem in result
