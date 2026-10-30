"""Long-lived *versioned* Support Worker (``make worker-support-versioned``).

The same Worker Deployment Version as the AgentCore entrypoint
(``support-supervisor:<BUILD_ID>`` on ``support-tq``, PINNED) but without the debounce:
it polls until Ctrl-C. Uses:

- v0.7: compare the serverless shape with an always-on Worker of the same version;
  make a version current without ``--allow-no-pollers``.
- v0.8: the local poller that lets ``create-version`` / ``set-current-version`` succeed
  while the AgentCore runtime cannot reach the laptop (plan v0.8 step 6).

Shape follows samples-python ``worker_versioning/workerv1.py`` (versioned Worker,
envconfig client) with the pinned StrandsPlugin of ``worker.py``.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from support_agent import config, mtls
from support_agent.worker_factory import build_versioned_worker


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    mtls.load_mtls_into_env()
    client = await config.connect(plugins=[config.build_strands_plugin()])
    queue, name, build = config.versioned_task_queue(), config.deployment_name(), config.build_id()
    logging.info(
        "versioned support worker: namespace=%s task_queue=%s version=%s:%s (PINNED) model=%s",
        client.namespace,
        queue,
        name,
        build,
        config.model_id(),
    )
    with ThreadPoolExecutor(max_workers=16) as executor:
        worker = build_versioned_worker(
            client,
            task_queue=queue,
            deployment_name=name,
            build_id=build,
            activity_executor=executor,
            graceful_shutdown_timeout=config.GRACEFUL_SHUTDOWN_TIMEOUT,
        )
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
