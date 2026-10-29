import pytest

import finance_a2a_api

pytestmark = pytest.mark.unit


def test_published_names_match_adr_0004() -> None:
    assert finance_a2a_api.ENDPOINT == "finance-agent"
    assert finance_a2a_api.SERVICE == "FinanceAgentA2A"
