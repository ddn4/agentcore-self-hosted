"""A scripted Support team: answers each ``invoke_model`` by the caller's system prompt.

v0.3 runs three agents in one Workflow (the Supervisor and its two sub-agents),
and all of them share the Worker's single ``"bedrock"`` model. With one
``ScriptedModel`` per Worker, the script cannot be a flat list (the order the
calls interleave in is the agents' business), so this responder looks at
``ModelCall.system_prompt`` and plays that agent's part from the conversation it
was given. It reads the real tool results (order JSON, policy text), so replies
carry the real price and delivery age.

Test seam only (plan section 3.1). The prompts are passed in, so this package
does not depend on the service it tests.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from demo_testing.scripted_model import ModelCall, Responder, ToolCall, Turn

#: Prefix on every scripted final answer, so a scripted run is never mistaken for Opus.
SCRIPTED = "[scripted]"


@dataclass(frozen=True)
class SupportPrompts:
    """The three system prompts the responder dispatches on."""

    supervisor: str
    order_lookup: str
    return_policy: str


def _order_id(text: str) -> str:
    match = re.search(r"\d{3,}", text)
    return match.group(0) if match else ""


def _order_agent(call: ModelCall) -> Turn:
    results = call.tool_results
    if not results:
        return ToolCall("lookup_order", {"order_id": _order_id(call.last_user_text)})
    try:
        order = json.loads(results[-1])
    except json.JSONDecodeError:
        return f"Order {_order_id(call.last_user_text)} was not found: {results[-1]}"
    return (
        f"Order {order['order_id']}: {order['item']}, ${order['price_usd']:.2f}, "
        f"delivered {order['delivered_on']} ({order['days_since_delivery']} days ago)."
    )


def _policy_agent(call: ModelCall) -> Turn:
    if not call.tool_results:
        return ToolCall("get_return_policy", {})
    question = call.last_user_text
    price = re.search(r"\$(\d+(?:\.\d+)?)", question)
    days = re.search(r"\((\d+) days ago\)", question)
    if not price or not days:
        return "NOT_ELIGIBLE: the order facts are missing, so eligibility cannot be decided."
    if int(days.group(1)) > 30:
        return "NOT_ELIGIBLE: reported more than 30 days after delivery (rule 3)."
    answer = "ELIGIBLE: damaged items reported within 30 days are refundable (rule 1)."
    if float(price.group(1)) > 100:
        answer += (
            " FINANCE_MANAGER_APPROVAL_REQUIRED: refunds over $100 need a Finance manager (rule 5)."
        )
    return answer


def _supervisor(call: ModelCall) -> Turn:
    ticket = call.last_user_text.removeprefix("Customer ticket: ")
    results = call.tool_results
    if not results:
        return ToolCall("order_lookup_agent", {"question": f"Look up order {_order_id(ticket)}."})
    if len(results) == 1:
        if "not found" in results[0]:
            return (
                f"{SCRIPTED} Sorry, I couldn't find that order. Could you check the order number?"
            )
        return ToolCall(
            "return_policy_agent",
            {"question": f"{results[0]} Customer's reason: {ticket}"},
        )
    facts, decision = results[0], results[1]
    if decision.startswith("NOT_ELIGIBLE"):
        return f"{SCRIPTED} {facts} Unfortunately this is not eligible for a refund: {decision}"
    if "FINANCE_MANAGER_APPROVAL_REQUIRED" in decision:
        return (
            f"{SCRIPTED} {facts} It is eligible for a refund, and because it is over $100 "
            "I have escalated your request to a Finance manager for approval."
        )
    return f"{SCRIPTED} {facts} It is eligible for a refund; I have passed your request to Finance."


def support_responder(prompts: SupportPrompts) -> Responder:
    """A ``ScriptedModel`` responder that plays all three Support agents."""

    def respond(call: ModelCall) -> Turn:
        if call.system_prompt == prompts.order_lookup:
            return _order_agent(call)
        if call.system_prompt == prompts.return_policy:
            return _policy_agent(call)
        if call.system_prompt == prompts.supervisor:
            return _supervisor(call)
        raise AssertionError(f"unexpected system prompt: {call.system_prompt!r:.80}")

    return respond
