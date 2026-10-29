"""Finance Activities in an ActivityEnvironment (no server)."""

from __future__ import annotations

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from finance_agent.activities import (
    IssueRefundInput,
    fraud_score,
    issue_refund,
    refund_history,
)
from payments_mock.server import PaymentsServer

pytestmark = pytest.mark.unit


def test_fraud_score() -> None:
    env = ActivityEnvironment()
    low = env.run(fraud_score, "1042")
    assert low["risk"] == "LOW" and low["score"] < 0.6 and low["signals"]
    assert env.run(fraud_score, "#3301")["risk"] == "HIGH"
    unknown = env.run(fraud_score, "9999")
    assert unknown["order_id"] == "9999" and unknown["signals"] == ["no order on record"]


def test_refund_history() -> None:
    env = ActivityEnvironment()
    assert env.run(refund_history, "2077")["refunds_last_12_months"] == 1
    assert env.run(refund_history, "9999")["note"] == "no customer record for this order"


def test_issue_refund_twice_with_one_key_issues_one_refund(payments: PaymentsServer) -> None:
    env = ActivityEnvironment()
    req = IssueRefundInput(order_id="1042", amount_usd=40.0, idempotency_key="a2a-task-x")
    first = env.run(issue_refund, req)
    second = env.run(issue_refund, req)
    assert first.refund_id == second.refund_id
    assert (first.replayed, second.replayed) == (False, True)
    assert len(payments.ledger.all()) == 1
    assert first.handled_by.startswith("local-")


def test_issue_refund_reports_pod_name(
    payments: PaymentsServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POD_NAME", "agent-services-worker-7d9f-abcde")
    receipt = ActivityEnvironment().run(
        issue_refund, IssueRefundInput(order_id="1042", amount_usd=40.0, idempotency_key="k")
    )
    assert receipt.handled_by == "agent-services-worker-7d9f-abcde"


def test_issue_refund_conflict_is_not_retryable(payments: PaymentsServer) -> None:
    env = ActivityEnvironment()
    env.run(issue_refund, IssueRefundInput(order_id="1042", amount_usd=40.0, idempotency_key="k"))
    with pytest.raises(ApplicationError) as info:
        env.run(
            issue_refund, IssueRefundInput(order_id="1042", amount_usd=41.0, idempotency_key="k")
        )
    assert info.value.non_retryable and info.value.type == "RefundRefused"


def test_payments_down_is_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAYMENTS_URL", "http://127.0.0.1:9")  # discard port: refused
    with pytest.raises(ApplicationError) as info:
        ActivityEnvironment().run(
            issue_refund, IssueRefundInput(order_id="1042", amount_usd=40.0, idempotency_key="k")
        )
    assert not info.value.non_retryable and info.value.type == "PaymentsUnavailable"
