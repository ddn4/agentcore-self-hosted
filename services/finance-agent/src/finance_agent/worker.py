"""Finance Worker (``make worker-finance``): the A2A handler in ``agent-services``.

Polls ``FINANCE_TASK_QUEUE`` (default ``agent-services-tq``, the target of the
``finance-agent`` Nexus endpoint) for both Workflow/Activity tasks and **Nexus
tasks**. Unversioned for now (runbook 7b.5). Long-running: on EKS it is a
Deployment (v0.11); locally it runs until Ctrl-C.

Shape follows samples-python ``hello_nexus/handler/worker.py`` (Workflows plus
``nexus_service_handlers``) and ``strands_plugin/tools/run_worker.py``
(StrandsPlugin on the client, model pinned, ADR-0001); thread pool for the
synchronous Activities (temporal-developer ``python.md``).
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import Executor, ThreadPoolExecutor

from temporalio.client import Client
from temporalio.worker import Worker

from finance_agent import config
from finance_agent.activities import fraud_score, issue_refund, refund_history, say_hello
from finance_agent.service_handler import FinanceAgentA2AHandler
from finance_agent.workflows import FinanceAgentTaskWorkflow, HelloWorkflow

WORKFLOWS = [FinanceAgentTaskWorkflow, HelloWorkflow]
ACTIVITIES = [fraud_score, refund_history, issue_refund, say_hello]


def build_worker(
    client: Client,
    *,
    task_queue: str | None = None,
    activity_executor: Executor | None = None,
    **worker_kwargs: object,
) -> Worker:
    """The Finance Worker: Workflows, Activities and the FinanceAgentA2A Nexus handler.

    ``activity_executor`` runs the synchronous Activities; the caller owns its
    lifetime (a default one is created for short-lived test Workers).
    """
    return Worker(
        client,
        task_queue=task_queue or config.task_queue(),
        workflows=WORKFLOWS,
        activities=ACTIVITIES,
        nexus_service_handlers=[FinanceAgentA2AHandler()],
        activity_executor=activity_executor or ThreadPoolExecutor(max_workers=8),
        **worker_kwargs,  # type: ignore[arg-type]
    )


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    client = await config.connect(plugins=[config.build_strands_plugin()])
    logging.info(
        "finance worker: namespace=%s task_queue=%s model=%s (key %r) region=%s effort=%s "
        "payments=%s",
        client.namespace,
        config.task_queue(),
        config.model_id(),
        config.MODEL_KEY,
        config.aws_region(),
        config.effort(),
        config.payments_url(),
    )
    with ThreadPoolExecutor(max_workers=16) as executor:
        await build_worker(client, activity_executor=executor).run()


if __name__ == "__main__":
    asyncio.run(main())
