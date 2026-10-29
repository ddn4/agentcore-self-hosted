import pytest

import finance_a2a_api
from finance_a2a_api import FinanceAgentA2A

pytestmark = pytest.mark.unit


def test_published_names_match_adr_0004() -> None:
    assert finance_a2a_api.ENDPOINT == "finance-agent"
    assert finance_a2a_api.SERVICE == "FinanceAgentA2A"


def test_service_definition_operations() -> None:
    """The Nexus service is named SERVICE and has exactly the two A2A operations."""
    import nexusrpc

    defn = nexusrpc.get_service_definition(FinanceAgentA2A)
    assert defn is not None
    assert defn.name == finance_a2a_api.SERVICE
    assert sorted(defn.operation_definitions) == ["get_agent_card", "send_message"]
    send = defn.operation_definitions["send_message"]
    assert send.input_type is finance_a2a_api.A2AMessage
    assert send.output_type is finance_a2a_api.A2ATask
