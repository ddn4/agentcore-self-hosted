"""Mock payments system with an idempotent refund API (team Finance's neighbour).

- ``payments_mock.ledger``: ``RefundLedger``, the pure, thread-safe idempotency logic.
- ``payments_mock.server``: a stdlib HTTP server around it (``make payments-mock``,
  port 8081 locally; a Deployment next to the Finance Workers on kind/EKS).

API (JSON):

- ``POST /refunds`` with header ``Idempotency-Key`` and body
  ``{"order_id": "1042", "amount_usd": 40.0}`` → ``201`` with the new refund, or
  ``200`` with the *same* refund (``"replayed": true``) when the key was seen before.
  The same key with a different body → ``409``. No key → ``400``.
- ``GET /refunds`` (all refunds), ``GET /refunds/<refund_id>``, ``GET /healthz``.

State is in memory: restarting the mock forgets refunds (fine for a demo; the
Finance Workflow's idempotency key is still stable across its own retries).
"""
