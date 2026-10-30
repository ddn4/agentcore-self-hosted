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
import signal
from concurrent.futures import Executor, ThreadPoolExecutor
from datetime import timedelta

from temporalio.client import Client
from temporalio.worker import Worker

from finance_agent import config
from finance_agent.activities import fraud_score, issue_refund, refund_history, say_hello
from finance_agent.service_handler import FinanceAgentA2AHandler
from finance_agent.workflows import FinanceAgentTaskWorkflow, HelloWorkflow

WORKFLOWS = [FinanceAgentTaskWorkflow, HelloWorkflow]
ACTIVITIES = [fraud_score, refund_history, issue_refund, say_hello]
#: How long a stopping Worker lets in-flight Activities finish (k8s: below the pod's
#: terminationGracePeriodSeconds of 30 s, so a deleted pod drains instead of being killed).
GRACEFUL_SHUTDOWN_TIMEOUT = timedelta(seconds=20)

log = logging.getLogger(__name__)


def build_worker(
    client: Client,
    *,
    task_queue: str | None = None,
    activity_executor: Executor | None = None,
    approval_timeout: str | None = None,
    **worker_kwargs: object,
) -> Worker:
    """The Finance Worker: Workflows, Activities and the FinanceAgentA2A Nexus handler.

    ``activity_executor`` runs the synchronous Activities; the caller owns its
    lifetime (a default one is created for short-lived test Workers).
    ``approval_timeout`` defaults to ``FINANCE_APPROVAL_TIMEOUT`` (``config``).
    """
    return Worker(
        client,
        task_queue=task_queue or config.task_queue(),
        workflows=WORKFLOWS,
        activities=ACTIVITIES,
        nexus_service_handlers=[
            FinanceAgentA2AHandler(approval_timeout=approval_timeout or config.approval_timeout())
        ],
        activity_executor=activity_executor or ThreadPoolExecutor(max_workers=8),
        **worker_kwargs,  # type: ignore[arg-type]
    )


async def run_until_signalled(worker: Worker) -> None:
    """Run ``worker`` until SIGTERM/SIGINT, then shut it down gracefully.

    On Kubernetes the Worker is PID 1, which ignores SIGTERM unless a handler is
    installed; without this a deleted pod keeps polling until SIGKILL 30 s later.
    Same shape as samples-python ``gcp/cloud_run/worker.py``: the signal handler
    schedules ``worker.shutdown()``, which stops polling, waits up to
    ``graceful_shutdown_timeout`` for in-flight Activities, and makes ``run()`` return.
    """
    loop = asyncio.get_running_loop()
    shutdown: list[asyncio.Task[None]] = []  # strong reference to the shutdown task

    def request_shutdown() -> None:
        if shutdown:
            return
        log.info("finance worker: shutdown requested; draining")
        shutdown.append(loop.create_task(worker.shutdown()))

    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, request_shutdown)
    try:
        await worker.run()
    finally:
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)
        if shutdown:
            await shutdown[0]
    log.info("finance worker: stopped")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    client = await config.connect(plugins=[config.build_strands_plugin()])
    logging.info(
        "finance worker: namespace=%s task_queue=%s model=%s (key %r) region=%s effort=%s "
        "payments=%s approval_timeout=%s",
        client.namespace,
        config.task_queue(),
        config.model_id(),
        config.MODEL_KEY,
        config.aws_region(),
        config.effort(),
        config.payments_url(),
        config.approval_timeout(),
    )
    with ThreadPoolExecutor(max_workers=16) as executor:
        worker = build_worker(
            client,
            activity_executor=executor,
            graceful_shutdown_timeout=GRACEFUL_SHUTDOWN_TIMEOUT,
        )
        await run_until_signalled(worker)


if __name__ == "__main__":
    asyncio.run(main())
