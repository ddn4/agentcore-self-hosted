"""Worker Versioning helpers for tests (v0.7).

``make_current_when_polled`` waits until a Worker of ``deployment:build`` has polled (the
version then exists) and makes it the Current Version, so Workflows started on its task
queue route to it. Same approach as samples-python ``worker_versioning/app.py``
(``wait_for_worker_and_make_current``: ``DescribeWorkerDeployment`` until the version is
listed, then ``SetWorkerDeploymentCurrentVersion``).
"""

from __future__ import annotations

import asyncio

import temporalio.api.workflowservice.v1 as wsv1
from temporalio.client import Client
from temporalio.service import RPCError


async def version_listed(client: Client, deployment_name: str, build_id: str) -> bool:
    try:
        resp = await client.workflow_service.describe_worker_deployment(
            wsv1.DescribeWorkerDeploymentRequest(
                namespace=client.namespace, deployment_name=deployment_name
            )
        )
    except RPCError:
        return False  # deployment not created yet
    return any(
        v.deployment_version.build_id == build_id
        for v in resp.worker_deployment_info.version_summaries
    )


async def make_current_when_polled(
    client: Client, deployment_name: str, build_id: str, *, seconds: float = 30.0
) -> None:
    """Wait for the version to appear (a Worker polled), then set it current."""
    async with asyncio.timeout(seconds):
        while True:  # polling the server (as the sample does), not local state
            if await version_listed(client, deployment_name, build_id):
                break
            await asyncio.sleep(0.2)
    await client.workflow_service.set_worker_deployment_current_version(
        wsv1.SetWorkerDeploymentCurrentVersionRequest(
            namespace=client.namespace, deployment_name=deployment_name, build_id=build_id
        )
    )
