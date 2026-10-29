"""A scripted Support team: answers each ``invoke_model`` by the caller's system prompt.

v0.5: the Supervisor part also talks to Finance. In ``tool`` mode it calls
``ask_finance_agent`` after an ELIGIBLE decision and quotes the refund ID from
the Finance agent's answer; in ``handoff`` mode it calls the capture tool
``submit_refund_request``, ends its turn, and writes the reply when the
Workflow hands it Finance's decision. Which mode is read from the tools it was
given, so one responder serves both.

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
    """The system prompts the responder dispatches on."""

    supervisor: str
    order_lookup: str
    return_policy: str
    #: v0.5 handoff mode's Supervisor prompt (same part, ``submit_refund_request``).
    supervisor_handoff: str = ""


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


def _ticket(call: ModelCall) -> str:
    """The customer's ticket: the first user message, without the Agent Card brief."""
    for message in call.messages:
        if message.get("role") != "user":
            continue
        texts = [block["text"] for block in message.get("content", []) if "text" in block]
        if texts:
            return texts[0].removeprefix("Customer ticket: ").split("\n\n")[0]
    return ""


def _latest_user_text(call: ModelCall) -> str:
    """Text of the newest message if it is a user *text* message (a re-prompt or handoff)."""
    if len(call.messages) < 2:
        return ""
    last = call.messages[-1]
    if last.get("role") != "user":
        return ""
    return "\n".join(block["text"] for block in last.get("content", []) if "text" in block)


def _refund_args(facts: str, ticket: str) -> dict[str, object]:
    price = re.search(r"\$(\d+(?:\.\d+)?)", facts)
    order_id = _order_id(facts) or _order_id(ticket)
    return {
        "order_id": order_id,
        "amount_usd": float(price.group(1)) if price else 0.0,
        "reason": "damaged",
        "summary": f"Customer reports order {order_id} arrived damaged; Support found it eligible.",
    }


def _final_reply(facts: str, finance: str) -> str:
    """The Supervisor's last word, from the Finance agent's answer."""
    refund = re.search(r"\bR-[A-Z0-9]+(?:-[A-Z0-9]+)*", finance)
    if "state=completed" in finance and refund:
        return (
            f"{SCRIPTED} {facts} It is eligible for a refund, and the Finance agent has "
            f"issued refund {refund.group(0)}."
        )
    if "could not process" in finance:
        return (
            f"{SCRIPTED} {facts} It is eligible, but I'm sorry: the Finance agent could not "
            "process the refund. A member of the Finance team will follow up."
        )
    return (
        f"{SCRIPTED} {facts} It is eligible, but the Finance agent did not approve it "
        f"automatically, so I have escalated it to a Finance manager. Finance said: {finance}"
    )


def _supervisor(call: ModelCall) -> Turn:
    ticket = _ticket(call)
    results = call.tool_results
    finance_tool = next(
        (t for t in ("ask_finance_agent", "submit_refund_request") if t in call.tool_names), None
    )
    latest = _latest_user_text(call)
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
    if finance_tool is None:  # v0.3 shape: no Finance tool
        if "FINANCE_MANAGER_APPROVAL_REQUIRED" in decision:
            return (
                f"{SCRIPTED} {facts} It is eligible for a refund, and because it is over $100 "
                "I have escalated your request to a Finance manager for approval."
            )
        return f"{SCRIPTED} {facts} It is eligible; I have passed your request to Finance."
    if len(results) == 2:
        return ToolCall(finance_tool, _refund_args(facts, ticket))
    if finance_tool == "submit_refund_request":
        if latest.startswith("Finance's decision"):
            return _final_reply(facts, latest)
        return f"{SCRIPTED} Your refund request is with the Finance team."
    return _final_reply(facts, results[2])


def support_responder(prompts: SupportPrompts) -> Responder:
    """A ``ScriptedModel`` responder that plays all three Support agents."""

    def respond(call: ModelCall) -> Turn:
        if call.system_prompt == prompts.order_lookup:
            return _order_agent(call)
        if call.system_prompt == prompts.return_policy:
            return _policy_agent(call)
        if call.system_prompt in (prompts.supervisor, prompts.supervisor_handoff):
            return _supervisor(call)
        raise AssertionError(f"unexpected system prompt: {call.system_prompt!r:.80}")

    return respond
