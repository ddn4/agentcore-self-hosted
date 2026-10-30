"""SIGTERM drains the Finance Worker (v0.9: Finance runs as PID 1 in a k8s pod).

PID 1 ignores SIGTERM unless a handler is installed, so before v0.9 a deleted Finance
pod kept polling (and could take tasks) until SIGKILL 30 s later. ``run_until_signalled``
follows samples-python ``gcp/cloud_run/worker.py``: the handler schedules
``worker.shutdown()`` and ``run()`` returns. Here the signal is sent to this process
while a real Worker polls the shared two-namespace dev server.
"""

import asyncio
import os
import signal
import uuid
from datetime import timedelta

import pytest
from temporalio.client import Client

from finance_agent.worker import build_worker, run_until_signalled
from finance_agent.workflows import HelloWorkflow

pytestmark = pytest.mark.integration


async def test_sigterm_stops_the_worker_gracefully(agent_services_client: Client) -> None:
    task_queue = f"test-sigterm-{uuid.uuid4()}"
    worker = build_worker(
        agent_services_client,
        task_queue=task_queue,
        graceful_shutdown_timeout=timedelta(seconds=2),
    )
    running = asyncio.create_task(run_until_signalled(worker))
    result = await agent_services_client.execute_workflow(
        HelloWorkflow.run, "Dana", id=f"sigterm-{uuid.uuid4()}", task_queue=task_queue
    )
    assert result == "Hello, Dana, from namespace agent-services!"

    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(running, timeout=15)
    assert worker.is_shutdown
    # The handlers were removed again: the next SIGTERM would not reach this Worker.
    assert asyncio.get_running_loop().remove_signal_handler(signal.SIGTERM) is False
