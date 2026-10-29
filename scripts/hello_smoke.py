"""Run each service's HelloWorkflow against the running dev server.

Connects exactly as the services' Workers do (envconfig: TEMPORAL_CONFIG_FILE,
TEMPORAL_PROFILE, env overrides; the pinned StrandsPlugin on the client), starts
an in-process Worker per service in its own namespace, executes HelloWorkflow and
prints the result. Used by scripts/verify/*.sh. Shape follows samples-python
hello/hello_activity.py.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractAsyncContextManager
from types import ModuleType
from typing import Any

from finance_agent import config as finance_config
from finance_agent import worker as finance_worker
from finance_agent.workflows import HelloWorkflow as FinanceHello
from support_agent import config as support_config
from support_agent import worker_factory as support_factory
from support_agent.workflows import HelloWorkflow as SupportHello

_executor = ThreadPoolExecutor(max_workers=4)


def support_worker(client: Any) -> AbstractAsyncContextManager[Any]:
    return support_factory.build_worker(
        client, task_queue=support_config.task_queue(), activity_executor=_executor
    )


def finance_worker_(client: Any) -> AbstractAsyncContextManager[Any]:
    return finance_worker.build_worker(client)


async def run(name: str, config: ModuleType, make_worker: Any, wf: Any) -> str:
    client = await config.connect(plugins=[config.build_strands_plugin()])
    workflow_id = f"hello-smoke-{name}-{uuid.uuid4().hex[:8]}"
    async with make_worker(client):
        result = await client.execute_workflow(
            wf.run, name, id=workflow_id, task_queue=config.task_queue()
        )
    print(
        f"{name}: namespace={client.namespace} task_queue={config.task_queue()} "
        f"workflow_id={workflow_id} result={result!r}"
    )
    return result


async def main() -> int:
    support = await run("support", support_config, support_worker, SupportHello)
    finance = await run("finance", finance_config, finance_worker_, FinanceHello)
    ok = support.endswith("namespace agents!") and finance.endswith("namespace agent-services!")
    print("hello-smoke:", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
