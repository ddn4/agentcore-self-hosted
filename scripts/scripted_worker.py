"""Offline stand-in for ``make worker-support``: same Worker, scripted model.

    uv run python scripts/scripted_worker.py [--delay 4]     (make worker-support-scripted)

Builds the Worker with ``support_agent.worker_factory`` exactly like
``support_agent.worker``, but the StrandsPlugin's ``"bedrock"`` key is bound to
a ``demo_testing.ScriptedModel`` that plays the Case A conversation (look up the
order, read the policy, answer from the real tool results) and "thinks" for
``--delay`` seconds per turn. Used by the v0.2 verify script to kill -9 a real
Worker process mid-``invoke_model`` without AWS. Test seam only: the service
itself has no scripted mode.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor

from demo_testing.scripted_model import ModelCall, ScriptedModel, ToolCall, Turn, scripted_plugin
from support_agent import config
from support_agent.worker_factory import build_worker


def case_a(call: ModelCall) -> Turn:
    results = call.tool_results
    if not results:
        match = re.search(r"\d{3,}", call.last_user_text)
        return ToolCall("lookup_order", {"order_id": match.group(0) if match else ""})
    if len(results) == 1:
        return ToolCall("get_return_policy", {})
    order = json.loads(results[0])
    eligible = order["days_since_delivery"] <= 30
    return (
        f"[scripted] Sorry your {order['item']} (${order['price_usd']:.2f}) arrived damaged. "
        f"It was delivered {order['days_since_delivery']} days ago, so it is "
        f"{'eligible' if eligible else 'not eligible'} for a refund"
        f"{'; I have passed your request to the Finance team.' if eligible else '.'}"
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Support Worker with a scripted model")
    parser.add_argument("--delay", type=float, default=4.0, help="seconds per model turn")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    client = await config.connect(
        plugins=[scripted_plugin(ScriptedModel(case_a, delay=args.delay))]
    )
    logging.info(
        "scripted support worker: namespace=%s task_queue=%s model=scripted (key 'bedrock')",
        client.namespace,
        config.task_queue(),
    )
    with ThreadPoolExecutor(max_workers=4) as executor:
        await build_worker(client, task_queue=config.task_queue(), activity_executor=executor).run()


if __name__ == "__main__":
    asyncio.run(main())
