"""Unit checks on the Support team's agent definitions (no server, no model)."""

from __future__ import annotations

import pytest

from support_agent import agents, config

pytestmark = pytest.mark.unit


def test_agents_use_the_key_the_worker_pins() -> None:
    # agents.py is Workflow code and cannot import config (boto3); keep them in step.
    assert agents.MODEL_KEY == config.MODEL_KEY == "bedrock"


def test_supervisor_tools_are_the_two_sub_agents() -> None:
    tools = agents.sub_agent_tools(lambda _run: None)
    assert [t.tool_name for t in tools] == ["order_lookup_agent", "return_policy_agent"]
    for name in ("order_lookup_agent", "return_policy_agent"):
        assert name in agents.SUPERVISOR_PROMPT
    # The model sees each tool's docstring and its one argument.
    specs = {t.tool_name: t.tool_spec for t in tools}
    for spec in specs.values():
        assert spec["inputSchema"]["json"]["required"] == ["question"]
    assert "Order Lookup agent" in specs["order_lookup_agent"]["description"]


def test_prompts_are_distinct_and_each_mode_names_its_finance_tool() -> None:
    prompts = {
        agents.SUPERVISOR_PROMPT,
        agents.SUPERVISOR_HANDOFF_PROMPT,
        agents.ORDER_LOOKUP_PROMPT,
        agents.RETURN_POLICY_PROMPT,
    }
    assert len(prompts) == 4  # the scripted model dispatches on them
    assert "ask_finance_agent" in agents.SUPERVISOR_PROMPT
    assert "submit_refund_request" not in agents.SUPERVISOR_PROMPT
    assert "submit_refund_request" in agents.SUPERVISOR_HANDOFF_PROMPT
    assert "ask_finance_agent" not in agents.SUPERVISOR_HANDOFF_PROMPT
    for prompt in (agents.SUPERVISOR_PROMPT, agents.SUPERVISOR_HANDOFF_PROMPT):
        assert "CANNOT issue refunds" in prompt
        assert "R-..." in prompt  # quote Finance's refund ID
    assert "FINANCE_MANAGER_APPROVAL_REQUIRED" in agents.RETURN_POLICY_PROMPT


async def _never(_req: agents.RefundRequest) -> str:
    raise AssertionError("not called")


@pytest.mark.parametrize(
    "tool",
    [
        agents.ask_finance_agent_tool(_never),
        agents.submit_refund_request_tool(lambda _req: None),
    ],
    ids=["ask_finance_agent", "submit_refund_request"],
)
def test_finance_tools_take_the_refund_facts_and_never_force_a_choice(tool: object) -> None:
    from strands.types.tools import AgentTool

    assert isinstance(tool, AgentTool)
    schema = tool.tool_spec["inputSchema"]["json"]
    assert schema["required"] == ["order_id", "amount_usd", "reason", "summary"]
    assert schema["properties"]["amount_usd"]["type"] == "number"
    assert "Finance" in tool.tool_spec["description"]


async def test_submit_refund_request_only_records() -> None:
    seen: list[agents.RefundRequest] = []
    tool = agents.submit_refund_request_tool(seen.append)
    result = await tool("1042", 40.0, "damaged", "Broken headphones.")  # type: ignore[operator]
    assert seen == [agents.RefundRequest("1042", 40.0, "damaged", "Broken headphones.")]
    assert result.startswith("Recorded.")
