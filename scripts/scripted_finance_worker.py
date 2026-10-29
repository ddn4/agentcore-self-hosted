"""Offline stand-in for ``make worker-finance``: same Finance Worker, scripted model.

    uv run python scripts/scripted_finance_worker.py [--delay 2]   (make worker-finance-scripted)

Builds the Worker with ``finance_agent.worker.build_worker`` exactly like
``finance_agent.worker`` (Workflows, Activities, the ``FinanceAgentA2A`` Nexus
handler), but the StrandsPlugin's ``"bedrock"`` key is bound to a
``demo_testing.ScriptedModel`` that plays the Finance Refund agent
(``demo_testing.finance_script``) from the real tool results. Used by the
verify script and for demos without AWS. Test seam only.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from demo_testing.finance_script import finance_responder
from demo_testing.scripted_model import ScriptedModel, scripted_plugin
from finance_agent import agents, config
from finance_agent.worker import build_worker


async def main() -> None:
    parser = argparse.ArgumentParser(description="Finance Worker with a scripted model")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds per model turn")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    model = ScriptedModel(finance_responder(agents.FINANCE_PROMPT), delay=args.delay)
    client = await config.connect(plugins=[scripted_plugin(model)])
    logging.info(
        "scripted finance worker: namespace=%s task_queue=%s model=scripted (key 'bedrock') "
        "payments=%s",
        client.namespace,
        config.task_queue(),
        config.payments_url(),
    )
    with ThreadPoolExecutor(max_workers=8) as executor:
        await build_worker(client, activity_executor=executor).run()


if __name__ == "__main__":
    asyncio.run(main())
