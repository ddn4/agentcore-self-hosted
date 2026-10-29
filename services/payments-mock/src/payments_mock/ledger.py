"""Idempotent refund ledger (pure logic; no I/O)."""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from typing import Any


class IdempotencyConflictError(Exception):
    """The idempotency key was already used for a different refund request."""


@dataclass(frozen=True)
class Refund:
    refund_id: str
    order_id: str
    amount_usd: float
    idempotency_key: str

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class RefundLedger:
    """Issues each refund at most once per idempotency key.

    ``issue`` returns ``(refund, replayed)``: ``replayed`` is True when the key was
    seen before and the original refund is returned unchanged. Thread-safe, because
    the HTTP server handles requests on several threads.
    """

    def __init__(self, first_number: int = 10001) -> None:
        self._lock = threading.Lock()
        self._next = first_number
        self._by_key: dict[str, Refund] = {}
        self._by_id: dict[str, Refund] = {}

    def issue(
        self, *, idempotency_key: str, order_id: str, amount_usd: float
    ) -> tuple[Refund, bool]:
        if not idempotency_key:
            raise ValueError("an idempotency key is required")
        if amount_usd <= 0:
            raise ValueError("amount_usd must be positive")
        with self._lock:
            existing = self._by_key.get(idempotency_key)
            if existing is not None:
                if existing.order_id != order_id or existing.amount_usd != amount_usd:
                    raise IdempotencyConflictError(
                        f"key {idempotency_key!r} was used for order {existing.order_id} "
                        f"${existing.amount_usd:.2f}"
                    )
                return existing, True
            refund = Refund(
                refund_id=f"R-{self._next}",
                order_id=order_id,
                amount_usd=amount_usd,
                idempotency_key=idempotency_key,
            )
            self._next += 1
            self._by_key[idempotency_key] = refund
            self._by_id[refund.refund_id] = refund
            return refund, False

    def get(self, refund_id: str) -> Refund | None:
        with self._lock:
            return self._by_id.get(refund_id)

    def all(self) -> list[Refund]:
        with self._lock:
            return list(self._by_id.values())
