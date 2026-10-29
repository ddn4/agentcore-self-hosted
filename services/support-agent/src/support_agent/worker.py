"""Local, unversioned Support Worker (``make worker-support``).

Polls ``SUPPORT_TASK_QUEUE`` (default ``support-dev-tq``) in namespace ``agents``
until Ctrl-C. Kill it mid-run and start it again: the Workflow carries on from
the last recorded Activity (the v0.2 crash-safety demo).

Connection via envconfig (``TEMPORAL_CONFIG_FILE`` / ``TEMPORAL_PROFILE``), model
pinned in the StrandsPlugin (ADR-0001). Shape follows samples-python
``strands_plugin/hello_world/run_worker.py`` and the temporal-developer skill's
``python.md`` worker (thread pool for sync Activities).
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from support_agent import config
from support_agent.worker_factory import build_worker


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    client = await config.connect(plugins=[config.build_strands_plugin()])
    logging.info(
        "support worker: namespace=%s task_queue=%s model=%s (key %r) region=%s effort=%s",
        client.namespace,
        config.task_queue(),
        config.model_id(),
        config.MODEL_KEY,
        config.aws_region(),
        config.effort(),
    )
    with ThreadPoolExecutor(max_workers=16) as executor:
        worker = build_worker(client, task_queue=config.task_queue(), activity_executor=executor)
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
