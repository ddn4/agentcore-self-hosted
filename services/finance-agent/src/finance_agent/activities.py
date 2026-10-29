"""Activities for team Finance (handler), namespace ``agent-services``.

- ``fraud_score`` and ``refund_history``: the Finance Refund agent's tools
  (``activity_as_tool``). They read Finance-only mock data that Support never
  sees (runbook 1.4). The docstrings are what the model sees.
- ``issue_refund``: called by Workflow code (not by the model) once the
  deterministic approval rule allows it. It calls the payments system with an
  **idempotency key**, so a retried Activity never refunds twice (runbook 7b.4).

All three block (file reads, HTTP), so they are **synchronous** Activities run on
the Worker's ``activity_executor`` thread pool (temporal-developer
``sync-vs-async.md``). ``httpx`` client retries stay off: Temporal owns retries.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Any

import httpx
from temporalio import activity
from temporalio.exceptions import ApplicationError

from finance_agent import config

_DATA = resources.files("finance_agent") / "data"
PAYMENTS_TIMEOUT_SECONDS = 10.0


@cache
def _load(name: str) -> dict[str, Any]:
    return json.loads((_DATA / name).read_text(encoding="utf-8"))


def _order_id(order_id: str) -> str:
    return order_id.strip().lstrip("#")


@activity.defn
async def say_hello(name: str) -> str:
    # Pure string formatting: nothing blocks, so an async Activity is safe here.
    return f"Hello, {name}, from namespace agent-services!"


@activity.defn
def fraud_score(order_id: str) -> dict[str, Any]:
    """Score the fraud risk of a refund request for an order (Finance data).

    Args:
        order_id: The order number, digits only (for example "1042").

    Returns:
        order_id, score from 0 (safe) to 1 (fraud), risk LOW or HIGH, and the
        signals behind the score.
    """
    data = _load("fraud.json")
    oid = _order_id(order_id)
    entry = data["orders"].get(oid, data["unknown"])
    score = float(entry["score"])
    risk = "HIGH" if score >= float(data["high_risk_threshold"]) else "LOW"
    activity.logger.info("fraud_score %s: %.2f %s", oid, score, risk)
    return {"order_id": oid, "score": score, "risk": risk, "signals": list(entry["signals"])}


@activity.defn
def refund_history(order_id: str) -> dict[str, Any]:
    """Look up the refund history of the customer who placed an order (Finance data).

    Args:
        order_id: The order number, digits only (for example "1042").

    Returns:
        order_id, refunds in the last 12 months and total refunded in USD.
    """
    oid = _order_id(order_id)
    entry = _load("refund_history.json")["orders"].get(oid)
    if entry is None:
        return {
            "order_id": oid,
            "refunds_last_12_months": 0,
            "total_refunded_usd": 0.0,
            "note": "no customer record for this order",
        }
    activity.logger.info("refund_history %s: %d refunds", oid, entry["refunds_last_12_months"])
    return {
        "order_id": oid,
        "refunds_last_12_months": int(entry["refunds_last_12_months"]),
        "total_refunded_usd": float(entry["total_refunded_usd"]),
    }


@dataclass
class IssueRefundInput:
    order_id: str
    amount_usd: float
    #: Stable across Activity retries and Workflow replays: the A2A task ID.
    idempotency_key: str


@dataclass
class RefundReceipt:
    refund_id: str
    order_id: str
    amount_usd: float
    #: True when payments had already issued this key (a retry): no new money moved.
    replayed: bool
    #: Which Finance Worker issued it (pod name on k8s): shows the hop to EKS.
    handled_by: str


@activity.defn
def issue_refund(req: IssueRefundInput) -> RefundReceipt:
    """Issue the refund through the payments system, idempotently."""
    url = f"{config.payments_url()}/refunds"
    try:
        resp = httpx.post(
            url,
            json={"order_id": req.order_id, "amount_usd": req.amount_usd},
            headers={"Idempotency-Key": req.idempotency_key},
            timeout=PAYMENTS_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as err:
        # Payments down or slow: transient, let Temporal retry with backoff.
        raise ApplicationError(
            f"payments unreachable at {url}: {err}", type="PaymentsUnavailable"
        ) from err
    if resp.status_code in (400, 409):
        # A bad request or a key reused for a different refund: retrying can't fix it.
        raise ApplicationError(
            f"payments refused the refund ({resp.status_code}): {resp.text}",
            type="RefundRefused",
            non_retryable=True,
        )
    if resp.status_code >= 300:
        raise ApplicationError(
            f"payments error {resp.status_code}: {resp.text}", type="PaymentsError"
        )
    body = resp.json()
    activity.logger.info(
        "issue_refund %s: %s $%.2f%s",
        req.idempotency_key,
        body["refund_id"],
        req.amount_usd,
        " (replayed)" if body["replayed"] else "",
    )
    return RefundReceipt(
        refund_id=body["refund_id"],
        order_id=body["order_id"],
        amount_usd=float(body["amount_usd"]),
        replayed=bool(body["replayed"]),
        handled_by=config.handled_by(),
    )
