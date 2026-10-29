"""Nexus endpoint helpers for tests and scripts.

``WorkflowEnvironment.create_nexus_endpoint`` targets the environment client's
own namespace; this demo's endpoint crosses namespaces (caller ``agents`` ->
handler ``agent-services``), so these helpers take the target namespace
explicitly. Same calls as samples-python ``tests/helpers/nexus.py`` (operator
service ``CreateNexusEndpoint`` / ``DeleteNexusEndpoint``), plus the endpoint
description, which carries the Agent Card (runbook 7b.8).
"""

from __future__ import annotations

from temporalio.api.common.v1 import Payload
from temporalio.api.nexus.v1 import Endpoint, EndpointSpec, EndpointTarget
from temporalio.api.operatorservice.v1 import (
    CreateNexusEndpointRequest,
    DeleteNexusEndpointRequest,
)
from temporalio.client import Client
from temporalio.converter import default


async def create_nexus_endpoint(
    client: Client,
    *,
    name: str,
    target_namespace: str,
    task_queue: str,
    description: str | None = None,
) -> Endpoint:
    """Create an endpoint routing to ``task_queue`` in ``target_namespace``."""
    spec = EndpointSpec(
        name=name,
        target=EndpointTarget(
            worker=EndpointTarget.Worker(namespace=target_namespace, task_queue=task_queue)
        ),
    )
    if description is not None:
        # The CLI stores --description-file as a JSON string payload; do the same.
        payload: Payload = default().payload_converter.to_payloads([description])[0]
        spec.description.CopyFrom(payload)
    response = await client.operator_service.create_nexus_endpoint(
        CreateNexusEndpointRequest(spec=spec)
    )
    return response.endpoint


async def delete_nexus_endpoint(client: Client, endpoint: Endpoint) -> None:
    await client.operator_service.delete_nexus_endpoint(
        DeleteNexusEndpointRequest(id=endpoint.id, version=endpoint.version)
    )


def endpoint_description(endpoint: Endpoint) -> str:
    """The endpoint description as text (what the Web UI's Nexus page renders)."""
    if not endpoint.spec.HasField("description"):
        return ""
    return default().payload_converter.from_payloads([endpoint.spec.description], [str])[0]
