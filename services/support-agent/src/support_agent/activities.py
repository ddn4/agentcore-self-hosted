"""Activities for team Support (caller).

``lookup_order`` and ``get_return_policy`` are the Support Supervisor's tools.
The Workflow wraps them with ``activity_as_tool`` (samples-python
``strands_plugin/tools``), so every tool call is a Temporal Activity: it is
recorded in history, retried by Temporal and never re-run on replay.

They read mock data bundled in the package (``support_agent/data``), which is
blocking file I/O, so they are **synchronous** Activities run on the Worker's
``activity_executor`` thread pool (temporal-developer ``sync-vs-async.md``:
default to sync; never block the event loop in an ``async def`` Activity).
The docstrings are what the model sees as tool descriptions.
"""

from __future__ import annotations

import datetime
import json
import os
import socket
from importlib import resources
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

_DATA = resources.files("support_agent") / "data"


@activity.defn
async def say_hello(name: str) -> str:
    # Pure string formatting: nothing blocks, so an async Activity is safe here.
    return f"Hello, {name}, from namespace agents!"


@activity.defn
def lookup_order(order_id: str) -> dict[str, Any]:
    """Look up a customer order by its order number.

    Args:
        order_id: The order number, digits only (for example "1042").

    Returns:
        The order: id, customer, item, category, price in USD, delivery date,
        days since delivery and status.
    """
    order_id = order_id.strip().lstrip("#")
    orders: dict[str, dict[str, Any]] = json.loads(
        (_DATA / "orders.json").read_text(encoding="utf-8")
    )["orders"]
    order = orders.get(order_id)
    if order is None:
        # Retrying cannot make an unknown order appear: fail this attempt for
        # good. activity_as_tool hands the message to the model as a tool error.
        raise ApplicationError(
            f"No order with id {order_id!r}", type="OrderNotFound", non_retryable=True
        )
    days = int(order["delivered_days_ago"])
    delivered_on = datetime.date.today() - datetime.timedelta(days=days)
    activity.logger.info("lookup_order %s: %s", order_id, order["item"])
    return {
        "order_id": order["order_id"],
        "customer": order["customer"],
        "item": order["item"],
        "category": order["category"],
        "price_usd": order["price_usd"],
        "delivered_on": delivered_on.isoformat(),
        "days_since_delivery": days,
        "status": order["status"],
    }


@activity.defn
def get_return_policy() -> str:
    """Return the store's return and refund policy (markdown).

    Returns:
        The full policy text, including the 30-day window and who may issue refunds.
    """
    return (_DATA / "return_policy.md").read_text(encoding="utf-8")


@activity.defn
async def ping(note: str) -> str:
    """``PingWorkflow``'s one Activity: echo ``note`` with the process that ran it (v0.7).

    String formatting only, nothing blocks, so an async Activity is safe here.
    """
    return f"pong {note} from pid {os.getpid()} on {socket.gethostname()}"
