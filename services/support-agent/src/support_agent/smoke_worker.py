"""Smoke caller Worker (``make worker-smoke``): ``A2ASmokeWorkflow`` on ``nexus-smoke-tq``.

A separate, unversioned queue in namespace ``agents`` (runbook 7b.9, ADR-0004),
so it never hides the serverless behaviour of Support's own queues. No
StrandsPlugin: this caller has no LLM and uses the SDK's default data converter.
The contract types are plain dataclasses, so they read the same under the
plugin's pydantic converter on the Finance side
(``packages/finance-a2a-api/tests/test_roundtrip.py``).
"""

from __future__ import annotations

import asyncio
import logging
import os

from temporalio.client import Client
from temporalio.worker import Worker

from support_agent import config
from support_agent.smoke import A2ASmokeWorkflow

DEFAULT_SMOKE_TASK_QUEUE = "nexus-smoke-tq"


def smoke_task_queue() -> str:
    return os.environ.get("SMOKE_TASK_QUEUE", DEFAULT_SMOKE_TASK_QUEUE)


def build_smoke_worker(client: Client, *, task_queue: str | None = None) -> Worker:
    return Worker(client, task_queue=task_queue or smoke_task_queue(), workflows=[A2ASmokeWorkflow])


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    client = await config.connect()
    logging.info(
        "smoke worker: namespace=%s task_queue=%s (no LLM)", client.namespace, smoke_task_queue()
    )
    await build_smoke_worker(client).run()


if __name__ == "__main__":
    asyncio.run(main())
