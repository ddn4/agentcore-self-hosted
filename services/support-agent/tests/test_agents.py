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


def test_prompts_are_distinct_and_do_not_mention_finance_tools_yet() -> None:
    prompts = {agents.SUPERVISOR_PROMPT, agents.ORDER_LOOKUP_PROMPT, agents.RETURN_POLICY_PROMPT}
    assert len(prompts) == 3  # the scripted model dispatches on them
    assert "ask_finance_agent" not in agents.SUPERVISOR_PROMPT  # v0.5
    assert "CANNOT issue refunds" in agents.SUPERVISOR_PROMPT
    assert "FINANCE_MANAGER_APPROVAL_REQUIRED" in agents.RETURN_POLICY_PROMPT
