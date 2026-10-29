import threading

import pytest

from payments_mock.ledger import IdempotencyConflictError, RefundLedger

pytestmark = pytest.mark.unit


def test_same_key_returns_the_same_refund() -> None:
    ledger = RefundLedger()
    first, replayed1 = ledger.issue(idempotency_key="k", order_id="1042", amount_usd=40.0)
    second, replayed2 = ledger.issue(idempotency_key="k", order_id="1042", amount_usd=40.0)
    assert first == second
    assert (replayed1, replayed2) == (False, True)
    assert ledger.all() == [first]
    assert first.refund_id == "R-10001"


def test_new_key_new_refund() -> None:
    ledger = RefundLedger()
    a, _ = ledger.issue(idempotency_key="a", order_id="1042", amount_usd=40.0)
    b, _ = ledger.issue(idempotency_key="b", order_id="1042", amount_usd=40.0)
    assert a.refund_id != b.refund_id
    assert ledger.get(b.refund_id) == b


def test_key_reuse_with_different_body_is_a_conflict() -> None:
    ledger = RefundLedger()
    ledger.issue(idempotency_key="k", order_id="1042", amount_usd=40.0)
    with pytest.raises(IdempotencyConflictError):
        ledger.issue(idempotency_key="k", order_id="1042", amount_usd=41.0)


@pytest.mark.parametrize(("key", "amount"), [("", 40.0), ("k", 0.0), ("k", -1.0)])
def test_rejects_bad_input(key: str, amount: float) -> None:
    with pytest.raises(ValueError):
        RefundLedger().issue(idempotency_key=key, order_id="1042", amount_usd=amount)


def test_concurrent_same_key_issues_once() -> None:
    ledger = RefundLedger()
    results = []

    def go() -> None:
        results.append(ledger.issue(idempotency_key="k", order_id="1", amount_usd=1.0)[0])

    threads = [threading.Thread(target=go) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ledger.all()) == 1
    assert len({r.refund_id for r in results}) == 1
