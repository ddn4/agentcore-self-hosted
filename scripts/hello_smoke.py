"""Run each service's HelloWorkflow against the running dev server.

Connects exactly as the services' Workers do (envconfig: TEMPORAL_CONFIG_FILE,
TEMPORAL_PROFILE, env overrides), starts an in-process Worker per service in its
own namespace, executes HelloWorkflow and prints the result. Used by
scripts/verify/v0.1-scaffold.sh. Shape follows samples-python hello/hello_activity.py.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from types import ModuleType

from finance_agent import config as finance_config
from finance_agent import worker as finance_worker
from support_agent import config as support_config
from support_agent import worker as support_worker


async def run(name: str, config: ModuleType, worker_mod: ModuleType) -> str:
    client = await config.connect()
    workflow_id = f"hello-smoke-{name}-{uuid.uuid4().hex[:8]}"
    async with worker_mod.build_worker(client):
        result = await client.execute_workflow(
            worker_mod.HelloWorkflow.run,
            name,
            id=workflow_id,
            task_queue=config.task_queue(),
        )
    print(
        f"{name}: namespace={client.namespace} task_queue={config.task_queue()} "
        f"workflow_id={workflow_id} result={result!r}"
    )
    return result


async def main() -> int:
    support = await run("support", support_config, support_worker)
    finance = await run("finance", finance_config, finance_worker)
    ok = support.endswith("namespace agents!") and finance.endswith("namespace agent-services!")
    print("hello-smoke:", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
