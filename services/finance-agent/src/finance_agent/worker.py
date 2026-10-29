"""Worker entrypoint for team Finance (handler) (v0.1: HelloWorkflow only).

Shape follows samples-python strands_plugin/hello_world/run_worker.py, with the
connection loaded through envconfig (samples-python env_config) instead of a
hard-coded address, and the model pinned in the StrandsPlugin.
"""

import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from finance_agent import config
from finance_agent.activities import say_hello
from finance_agent.workflows import HelloWorkflow


def build_worker(client: Client) -> Worker:
    return Worker(
        client,
        task_queue=config.task_queue(),
        workflows=[HelloWorkflow],
        activities=[say_hello],
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    client = await config.connect(plugins=[config.build_strands_plugin()])
    logging.info(
        "worker: namespace=%s task_queue=%s model=%s",
        client.namespace,
        config.task_queue(),
        config.model_id(),
    )
    await build_worker(client).run()


if __name__ == "__main__":
    asyncio.run(main())
