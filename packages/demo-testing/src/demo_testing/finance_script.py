"""A scripted Finance Refund agent: plays Opus 5.5's part offline.

Turn 1 asks for both tools at once (``fraud_score`` and ``refund_history``, a
parallel tool-use turn); turn 2 answers ``RISK=<what fraud_score said>`` with a
one-line justification built from the real tool results. Test seam only, like
``support_script`` (plan §3.1); the prompt is passed in.

``risk_override`` makes the "model" claim a risk regardless of the data, to test
that the approval rule is enforced in code (e.g. a model saying LOW on a $450
refund still needs an approver), and ``""`` simulates a model that forgets the
``RISK=`` line (fail safe: treated as HIGH).
"""

from __future__ import annotations

import json
import re
from typing import Any

from demo_testing.scripted_model import ModelCall, Responder, ToolCall, Turn
from demo_testing.support_script import SCRIPTED


def _order_id(text: str) -> str:
    match = re.search(r"order (\d{3,})", text)
    return match.group(1) if match else ""


def finance_responder(finance_prompt: str, *, risk_override: str | None = None) -> Responder:
    def respond(call: ModelCall) -> Turn:
        if call.system_prompt != finance_prompt:
            raise AssertionError(f"unexpected system prompt: {call.system_prompt!r}")
        results = call.tool_results
        if not results:
            order_id = _order_id(call.last_user_text)
            return [
                ToolCall("fraud_score", {"order_id": order_id}),
                ToolCall("refund_history", {"order_id": order_id}),
            ]
        facts: dict[str, Any] = {}
        for raw in results:
            try:
                facts.update(json.loads(raw))
            except json.JSONDecodeError:
                facts.setdefault("errors", raw)
        risk = facts.get("risk", "HIGH") if risk_override is None else risk_override
        verdict = f"RISK={risk}\n" if risk else ""
        return (
            f"{verdict}{SCRIPTED} Fraud score {facts.get('score')} "
            f"({', '.join(map(str, facts.get('signals', [])))}); "
            f"{facts.get('refunds_last_12_months')} refunds in the last 12 months."
        )

    return respond
