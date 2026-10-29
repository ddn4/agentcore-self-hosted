"""Support Activities in isolation (ActivityEnvironment; temporal-developer testing.md)."""

import datetime

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from support_agent.activities import get_return_policy, lookup_order

pytestmark = pytest.mark.unit


def test_lookup_order_1042_is_the_case_a_headphones() -> None:
    order = ActivityEnvironment().run(lookup_order, "#1042")
    assert order["item"] == "Wireless headphones"
    assert order["price_usd"] == 40.0
    assert order["days_since_delivery"] <= 30
    delivered = datetime.date.fromisoformat(order["delivered_on"])
    assert (datetime.date.today() - delivered).days == order["days_since_delivery"]


def test_lookup_order_2077_is_the_case_b_laptop() -> None:
    order = ActivityEnvironment().run(lookup_order, "2077")
    assert (order["item"], order["price_usd"]) == ("Laptop", 450.0)


def test_unknown_order_is_non_retryable() -> None:
    with pytest.raises(ApplicationError) as err:
        ActivityEnvironment().run(lookup_order, "9999")
    assert err.value.non_retryable
    assert err.value.type == "OrderNotFound"


def test_return_policy_states_the_30_day_window_and_no_refunds_by_support() -> None:
    policy = ActivityEnvironment().run(get_return_policy)
    assert "30 days" in policy
    assert "cannot issue refunds" in policy
