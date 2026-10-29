"""Builds the Support Worker. Shared by every way this service runs.

v0.2: the local unversioned Worker (``worker.py``, queue ``support-dev-tq``).
v0.7 adds the versioned, short-lived AgentCore shape on top of the same factory,
so local and serverless Workers always register the same Workflows/Activities.

The StrandsPlugin lives on the *client* (``config.connect(plugins=[...])``);
Workers built from that client inherit it, which registers the plugin's
``invoke_model`` Activities and the pydantic data converter. Same shape as
samples-python ``strands_plugin/tools/run_worker.py``.
"""

from __future__ import annotations

from concurrent.futures import Executor

from temporalio.client import Client
from temporalio.worker import Worker

from support_agent.activities import get_return_policy, lookup_order, say_hello
from support_agent.workflows import HelloWorkflow, SupportSupervisorWorkflow

WORKFLOWS = [SupportSupervisorWorkflow, HelloWorkflow]
ACTIVITIES = [lookup_order, get_return_policy, say_hello]


def build_worker(
    client: Client,
    *,
    task_queue: str,
    activity_executor: Executor,
    **worker_kwargs: object,
) -> Worker:
    """A Worker for this service's Workflows and Activities.

    ``activity_executor`` runs the synchronous Activities (``lookup_order``,
    ``get_return_policy``); the caller owns its lifetime.
    """
    return Worker(
        client,
        task_queue=task_queue,
        workflows=WORKFLOWS,
        activities=ACTIVITIES,
        activity_executor=activity_executor,
        **worker_kwargs,  # type: ignore[arg-type]
    )
