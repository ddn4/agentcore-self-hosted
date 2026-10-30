"""The serverless Worker on a real dev server (v0.7; plan: "an integration test runs
PingWorkflow on a versioned Worker").

1. ``build_versioned_worker``: PingWorkflow runs on ``<deployment>:<build>`` and is PINNED.
2. The AgentCore entrypoint end to end, in process, with the real ``BedrockAgentCoreApp``
   and the real ``run_worker``: invoke -> HealthyBusy -> work -> debounce -> drain ->
   Healthy; a second invocation while polling is acknowledged without a second Worker;
   work that arrives after the drain waits until the next invocation starts lifetime 2.

Unique deployment names and task queues per test (the dev server is shared by the session,
samples-python ``tests/conftest.py`` style). The version is made current the way
samples-python ``worker_versioning/app.py`` does it (``demo_testing.versioning``).
"""

from __future__ import annotations

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from bedrock_agentcore.runtime.models import PingStatus
from temporalio.api.enums.v1 import EventType, VersioningBehavior
from temporalio.client import Client, Plugin

from demo_testing.versioning import make_current_when_polled
from support_agent import agentcore_app, config
from support_agent.ping import PingWorkflow
from support_agent.worker_factory import build_versioned_worker

pytestmark = pytest.mark.integration
#: Short, but long enough for the test to make the version current and start work: the
#: window starts when the Worker starts, as in AgentCore (the WCI invokes because work waits).
DEBOUNCE = 3.0


async def _identities(client: Client, workflow_id: str) -> set[str]:
    history = await client.get_workflow_handle(workflow_id).fetch_history()
    return {
        e.workflow_task_started_event_attributes.identity
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_STARTED
    }


async def test_ping_workflow_on_a_versioned_worker(agents_client: Client) -> None:
    deployment, build, queue = f"support-test-{uuid.uuid4().hex[:8]}", "b1", f"tq-{uuid.uuid4()}"
    with ThreadPoolExecutor(max_workers=2) as executor:
        async with build_versioned_worker(
            agents_client,
            task_queue=queue,
            deployment_name=deployment,
            build_id=build,
            activity_executor=executor,
        ):
            await make_current_when_polled(agents_client, deployment, build)
            handle = await agents_client.start_workflow(
                PingWorkflow.run, "versioned", id=f"ping-{uuid.uuid4()}", task_queue=queue
            )
            result = await asyncio.wait_for(handle.result(), timeout=15)
    assert result.startswith("pong versioned from pid ")
    assert result.endswith(f"[{deployment}:{build}]")
    info = (await handle.describe()).raw_description.workflow_execution_info.versioning_info
    assert info.behavior == VersioningBehavior.VERSIONING_BEHAVIOR_PINNED
    assert info.deployment_version.deployment_name == deployment
    assert info.deployment_version.build_id == build


async def test_agentcore_entrypoint_lifecycle(
    agents_client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployment, build = f"support-ac-{uuid.uuid4().hex[:8]}", "b7"
    queue = f"tq-{uuid.uuid4()}"
    monkeypatch.setenv("TEMPORAL_DEPLOYMENT_NAME", deployment)
    monkeypatch.setenv("TEMPORAL_BUILD_ID", build)
    monkeypatch.setenv("TEMPORAL_TASK_QUEUE", queue)
    monkeypatch.delenv("TEMPORAL_MTLS_SECRET_ID", raising=False)

    async def connect_to_test_server(plugins: list[Plugin] | None = None) -> Client:
        cfg = agents_client.config()
        cfg["plugins"] = [*cfg["plugins"], *(plugins or [])]
        return Client(**cfg)

    monkeypatch.setattr(config, "connect", connect_to_test_server)

    async def run(version: agentcore_app.WorkerVersion, lifetime: int) -> None:
        await agentcore_app.run_worker(version, lifetime, debounce=DEBOUNCE)

    app, host = agentcore_app.create_app(run)
    assert app.get_current_ping_status() == PingStatus.HEALTHY

    # Lifetime 1: the WCI (here: the test) invokes; the ack is immediate.
    ack = await host.invoke({"deploymentName": deployment, "buildId": build})
    assert ack["message"] == "worker starting" and ack["lifetime"] == 1
    assert app.get_current_ping_status() == PingStatus.HEALTHY_BUSY
    again = await host.invoke({"deploymentName": deployment, "buildId": build})
    assert again["message"] == "worker already polling"

    await make_current_when_polled(agents_client, deployment, build)
    first_handle = await agents_client.start_workflow(
        PingWorkflow.run, "one", id=f"ping-a-{uuid.uuid4()}", task_queue=queue
    )
    first = await asyncio.wait_for(first_handle.result(), timeout=10)
    assert first.endswith(f"[{deployment}:{build}]")

    # Idle for the debounce: drain, release the async task.
    await asyncio.wait_for(host.wait(), timeout=10)
    assert app.get_current_ping_status() == PingStatus.HEALTHY

    # Work that arrives with no Worker waits on the queue ...
    handle = await agents_client.start_workflow(
        PingWorkflow.run, "two", id=f"ping-b-{uuid.uuid4()}", task_queue=queue
    )
    await asyncio.sleep(DEBOUNCE)
    assert (await handle.describe()).raw_description.workflow_execution_info.history_length <= 2
    # ... until the next invocation starts lifetime 2, on the same version.
    ack2 = await host.invoke({"deploymentName": deployment, "buildId": build})
    assert ack2["lifetime"] == 2
    second = await asyncio.wait_for(handle.result(), timeout=10)
    assert second.endswith(f"[{deployment}:{build}]")
    await asyncio.wait_for(host.wait(), timeout=10)
    assert app.get_current_ping_status() == PingStatus.HEALTHY

    assert all(
        i.endswith("/agentcore-lifetime-1")
        for i in await _identities(agents_client, first_handle.id)
    )
    ids = await _identities(agents_client, handle.id)
    assert ids and all(i.endswith("/agentcore-lifetime-2") for i in ids)
