"""Offline stand-in for ``make worker-support``: same Worker, scripted model.

    uv run python scripts/scripted_worker.py [--delay 4]     (make worker-support-scripted)

Builds the Worker with ``support_agent.worker_factory`` exactly like
``support_agent.worker``, but the StrandsPlugin's ``"bedrock"`` key is bound to
a ``demo_testing.ScriptedModel`` that plays the whole Support team (v0.3:
Supervisor, Order Lookup agent, Return Policy agent; dispatched by system
prompt, see ``demo_testing.support_script``) from the real tool results, and
"thinks" for ``--delay`` seconds per model turn. Used by the verify scripts to
kill -9 a real Worker process mid-``invoke_model`` without AWS. Test seam only:
the service itself has no scripted mode.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from demo_testing.scripted_model import ScriptedModel, scripted_plugin
from demo_testing.support_script import SupportPrompts, support_responder
from support_agent import agents, config
from support_agent.worker_factory import build_worker

PROMPTS = SupportPrompts(
    supervisor=agents.SUPERVISOR_PROMPT,
    order_lookup=agents.ORDER_LOOKUP_PROMPT,
    return_policy=agents.RETURN_POLICY_PROMPT,
)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Support Worker with a scripted model")
    parser.add_argument("--delay", type=float, default=4.0, help="seconds per model turn")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    model = ScriptedModel(support_responder(PROMPTS), delay=args.delay)
    client = await config.connect(plugins=[scripted_plugin(model)])
    logging.info(
        "scripted support worker: namespace=%s task_queue=%s model=scripted (key 'bedrock')",
        client.namespace,
        config.task_queue(),
    )
    with ThreadPoolExecutor(max_workers=4) as executor:
        await build_worker(client, task_queue=config.task_queue(), activity_executor=executor).run()


if __name__ == "__main__":
    asyncio.run(main())
