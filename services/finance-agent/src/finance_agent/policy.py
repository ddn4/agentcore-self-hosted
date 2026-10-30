"""The Finance approval rule: **the LLM recommends, code enforces** (runbook 7b.4).

Pure functions, imported by Workflow code (deterministic, no I/O). The Finance
Refund agent's model writes a risk assessment that must contain ``RISK=LOW`` or
``RISK=HIGH``; the decision to move money is this ``if``, not the model's.

v0.4 answered a request that needs a human approver with task state
``rejected``. From v0.6 that branch is ``input-required``: the task Workflow
waits for an ``approve`` or ``reject`` signal carrying an
:class:`ApproverDecision` (ADR-0002), for at most the approval timeout.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from finance_a2a_api import AUTO_APPROVE_LIMIT_USD

Risk = Literal["LOW", "HIGH"]
Decision = Literal["auto_approve", "needs_approver"]

_RISK = re.compile(r"RISK\s*=\s*(LOW|HIGH)\b", re.IGNORECASE)


def parse_risk(assessment: str) -> Risk:
    """The model's ``RISK=`` verdict. Missing or ambiguous counts as HIGH (fail safe)."""
    verdicts = {m.upper() for m in _RISK.findall(assessment)}
    return "LOW" if verdicts == {"LOW"} else "HIGH"


def decide(amount_usd: float, risk: Risk) -> Decision:
    """Auto-approve only small, low-risk refunds; everything else needs a human."""
    if amount_usd > AUTO_APPROVE_LIMIT_USD or risk == "HIGH":
        return "needs_approver"
    return "auto_approve"


@dataclass
class ApproverDecision:
    """Input of the ``approve`` and ``reject`` signals: who decided, and why.

    One dataclass, not bare strings, so fields can be added later without
    breaking senders (samples-python ``message_passing/introduction``
    ``ApproveInput``). From the CLI: ``--input '{"approver": "dana.finance"}'``.
    """

    approver: str
    comment: str = ""


@dataclass(frozen=True)
class RefundRequest:
    """The data part of Support's A2A message, validated."""

    order_id: str
    amount_usd: float
    reason: str


def parse_refund_request(data: dict[str, Any] | None) -> RefundRequest | str:
    """The validated request, or a reason it is invalid (task state ``failed``)."""
    if not data:
        return "the message has no data part {order_id, amount_usd, reason}"
    order_id = str(data.get("order_id", "")).strip().lstrip("#")
    if not order_id:
        return "order_id is missing"
    try:
        amount = float(data.get("amount_usd"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return f"amount_usd is not a number: {data.get('amount_usd')!r}"
    if not amount > 0:
        return f"amount_usd must be positive, got {amount}"
    return RefundRequest(
        order_id=order_id, amount_usd=round(amount, 2), reason=str(data.get("reason", ""))
    )
