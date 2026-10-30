"""Builds the Support Worker. Shared by every way this service runs.

v0.2: the local unversioned Worker (``worker.py``, queue ``support-dev-tq``).
v0.7: ``build_versioned_worker`` adds Worker Versioning (PINNED) on top of the same
factory for the AgentCore shape (``agentcore_app``) and ``worker_versioned``, so local
and serverless Workers always register the same Workflows/Activities.

The StrandsPlugin lives on the *client* (``config.connect(plugins=[...])``);
Workers built from that client inherit it, which registers the plugin's
``invoke_model`` Activities and the pydantic data converter. Same shape as
samples-python ``strands_plugin/tools/run_worker.py``.
"""

from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import Executor
from datetime import timedelta

from temporalio.client import Client
from temporalio.common import VersioningBehavior, WorkerDeploymentVersion
from temporalio.worker import Interceptor, Worker, WorkerDeploymentConfig

from support_agent.activities import get_return_policy, lookup_order, ping, say_hello
from support_agent.ping import PingWorkflow
from support_agent.workflows import HelloWorkflow, SupportSupervisorWorkflow

WORKFLOWS = [SupportSupervisorWorkflow, HelloWorkflow, PingWorkflow]
ACTIVITIES = [lookup_order, get_return_policy, say_hello, ping]


def deployment_config(deployment_name: str, build_id: str) -> WorkerDeploymentConfig:
    """Worker Versioning for serverless Workers (runbook 5.2, plan v0.7).

    Serverless Workers require versioning; every Workflow they run is **PINNED** to
    the Build ID that started it, so a conversation that spans several AgentCore
    sessions (Case B) always resumes on the exact code it started on.
    """
    return WorkerDeploymentConfig(
        version=WorkerDeploymentVersion(deployment_name=deployment_name, build_id=build_id),
        use_worker_versioning=True,
        default_versioning_behavior=VersioningBehavior.PINNED,
    )


def build_versioned_worker(
    client: Client,
    *,
    task_queue: str,
    deployment_name: str,
    build_id: str,
    activity_executor: Executor,
    interceptors: Sequence[Interceptor] = (),
    graceful_shutdown_timeout: timedelta = timedelta(),
    **worker_kwargs: object,
) -> Worker:
    """The same Workflows and Activities as ``build_worker``, as a versioned Worker.

    Used by the AgentCore entrypoint (``agentcore_app``, with the ``ActivityTracker``
    and a 120 s drain) and by ``make worker-support-versioned`` (long-lived). Shape
    follows samples-python ``bedrock_agentcore/strands_agent/agentcore_worker.py``.
    """
    return build_worker(
        client,
        task_queue=task_queue,
        activity_executor=activity_executor,
        interceptors=list(interceptors),
        deployment_config=deployment_config(deployment_name, build_id),
        graceful_shutdown_timeout=graceful_shutdown_timeout,
        **worker_kwargs,
    )


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
