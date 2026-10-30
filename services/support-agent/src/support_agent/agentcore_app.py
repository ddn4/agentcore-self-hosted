"""The Support Worker in AgentCore Runtime shape (runbook §1.2 steps 4-6, 5.2; plan v0.7).

``BedrockAgentCoreApp`` serves ``POST /invocations`` and ``GET /ping`` on :8080. An
invocation (from the WCI, or from ``make invoke-local`` while we play the WCI) is
*not* Workflow input: it only asks for Worker capacity. The entrypoint

1. refuses to start a second Worker while one is polling in this session,
2. registers an async task (``app.add_async_task("temporal-worker")``), which turns
   ``/ping`` to ``HealthyBusy`` and keeps the session alive,
3. starts the Worker with ``asyncio.create_task(...)`` and acknowledges at once.

The Worker connects (fail fast: ``TEMPORAL_CONNECT_TIMEOUT_SECONDS``), polls the
versioned queue ``support-tq`` as Worker Deployment Version
``support-supervisor:<BUILD_ID>`` with **PINNED** default behaviour, and drains when no
Activity has started or finished for ``AGENTCORE_DEBOUNCE_SECONDS``
(``graceful_shutdown_timeout`` 120 s). ``app.complete_async_task`` runs in ``finally``
whatever happened (drained, crashed, could not connect), so ``/ping`` returns to
``Healthy`` and AgentCore can idle the session out; without it the session stays
``HealthyBusy`` until ``maxLifetime`` (8 h).

Follows samples-python ``bedrock_agentcore/strands_agent/agentcore_worker.py`` and the
docs page "Serverless Workers on Amazon Bedrock AgentCore Runtime - Python SDK"
(runtime handler). Differences, each for this demo: the connection comes from
envconfig with optional mTLS from Secrets Manager (runbook 5.3) instead of an API
key; the model is pinned (``config.build_strands_plugin``, ADR-0001) instead of the
sample's bare ``StrandsPlugin()``; there is a connect timeout; the WCI payload is
checked against this Worker's version; the guard and Worker lifetime live in a small
class (``AgentCoreWorkerHost``) so tests can drive them with a fake app.

Run locally: ``make agentcore-local`` (needs ``TEMPORAL_BUILD_ID``, which the Makefile
takes from ``scripts/build-id.sh``). In the CodeZip bundle the entrypoint file
``agentcore_worker.py`` imports ``app`` from here and calls :func:`main`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from temporalio.client import Client, Plugin

from support_agent import config, mtls
from support_agent.activity_tracker import ActivityTracker
from support_agent.worker_factory import build_versioned_worker

log = logging.getLogger("support_agent.agentcore")


class AsyncTaskApp(Protocol):
    """The part of ``BedrockAgentCoreApp`` the host uses (a fake in unit tests)."""

    def add_async_task(self, name: str, metadata: dict[str, Any] | None = None) -> int: ...

    def complete_async_task(self, task_id: int) -> bool: ...


@dataclass(frozen=True)
class WorkerVersion:
    """Who this Worker is: what the WCI's ``{deploymentName, buildId}`` must match."""

    deployment_name: str
    build_id: str
    task_queue: str

    @classmethod
    def from_env(cls) -> WorkerVersion:
        return cls(config.deployment_name(), config.build_id(), config.versioned_task_queue())


#: One Worker lifetime: connect, poll until idle, drain. Receives the lifetime number.
RunWorker = Callable[[WorkerVersion, int], Awaitable[None]]


class AgentCoreWorkerHost:
    """Single-start guard + one async task per Worker lifetime (runbook 5.2)."""

    def __init__(self, app: AsyncTaskApp, run_worker: RunWorker) -> None:
        self._app = app
        self._run_worker = run_worker
        self._worker: asyncio.Task[None] | None = None
        #: Worker lifetimes started in this session (process).
        self.lifetimes = 0

    @property
    def polling(self) -> bool:
        return self._worker is not None and not self._worker.done()

    async def invoke(self, payload: dict[str, Any] | None) -> dict[str, Any]:
        """The ``@app.entrypoint`` body: start a Worker (at most one) and acknowledge."""
        try:
            version = WorkerVersion.from_env()
        except (config.MissingBuildIdError, ValueError) as err:
            log.error("refusing to start a Worker: %s", err)
            return {"error": str(err)}
        mismatch = _payload_mismatch(payload or {}, version)
        if mismatch:
            # The WCI asked for a version this code is not: starting would poll the wrong
            # version while the WCI keeps invoking. Say so loudly instead (runbook §6).
            log.error("refusing to start a Worker: %s", mismatch)
            return {"error": mismatch}
        if self.polling:
            log.info("worker already polling %s (lifetime %d)", version.task_queue, self.lifetimes)
            return _ack("worker already polling", version, self.lifetimes)
        self.lifetimes += 1
        task_id = self._app.add_async_task(
            "temporal-worker", {"build_id": version.build_id, "lifetime": self.lifetimes}
        )
        self._worker = asyncio.create_task(self._run_until_idle(task_id, version, self.lifetimes))
        return _ack("worker starting", version, self.lifetimes)

    async def wait(self) -> None:
        """Wait for the current Worker lifetime (tests, scripts)."""
        if self._worker is not None:
            await asyncio.shield(self._worker)

    async def _run_until_idle(self, task_id: int, version: WorkerVersion, lifetime: int) -> None:
        """Own one Worker lifetime, and always release the async task."""
        try:
            await self._run_worker(version, lifetime)
        except Exception:
            # Nothing awaits this task, so an error would otherwise be swallowed.
            log.exception("worker lifetime %d failed", lifetime)
        finally:
            # Without this the session stays HealthyBusy until maxLifetime (8 h).
            self._app.complete_async_task(task_id)
            log.info("worker lifetime %d over; async task released", lifetime)


def _payload_mismatch(payload: dict[str, Any], version: WorkerVersion) -> str | None:
    """Check the WCI payload ``{deploymentName, buildId}`` (runbook §1.2 step 3)."""
    want = {"deploymentName": version.deployment_name, "buildId": version.build_id}
    for key, ours in want.items():
        theirs = payload.get(key)
        if theirs not in (None, "", ours):
            return f"invocation asks for {key}={theirs!r}, this Worker is {ours!r}"
    return None


def _ack(message: str, version: WorkerVersion, lifetime: int) -> dict[str, Any]:
    return {
        "message": message,
        "task_queue": version.task_queue,
        "deployment_name": version.deployment_name,
        "build_id": version.build_id,
        "lifetime": lifetime,
    }


async def connect_with_timeout(plugins: Sequence[Plugin], seconds: float) -> Client:
    """``config.connect`` bounded by ``TEMPORAL_CONNECT_TIMEOUT_SECONDS`` (fail fast)."""
    try:
        async with asyncio.timeout(seconds):
            return await config.connect(plugins=list(plugins))
    except TimeoutError:
        raise TimeoutError(f"could not connect to Temporal within {seconds:g}s") from None


def default_plugins() -> list[Plugin]:
    return [config.build_strands_plugin()]


async def run_worker(
    version: WorkerVersion,
    lifetime: int,
    *,
    plugins: Callable[[], Sequence[Plugin]] = default_plugins,
    connect: Callable[[Sequence[Plugin], float], Awaitable[Client]] = connect_with_timeout,
    debounce: float | None = None,
) -> None:
    """One Worker lifetime: mTLS (if configured), connect, poll until idle, drain."""
    mtls.load_mtls_into_env()
    debounce = config.debounce_seconds() if debounce is None else debounce
    client = await connect(plugins(), config.connect_timeout_seconds())
    tracker = ActivityTracker()
    identity = f"{os.getpid()}@{socket.gethostname()}/agentcore-lifetime-{lifetime}"
    log.info(
        "lifetime %d: polling %s/%s as %s:%s (PINNED), debounce %gs, identity %s",
        lifetime,
        client.namespace,
        version.task_queue,
        version.deployment_name,
        version.build_id,
        debounce,
        identity,
    )
    with ThreadPoolExecutor(max_workers=8) as activity_executor:
        worker = build_versioned_worker(
            client,
            task_queue=version.task_queue,
            deployment_name=version.deployment_name,
            build_id=version.build_id,
            activity_executor=activity_executor,
            interceptors=[tracker],
            graceful_shutdown_timeout=config.GRACEFUL_SHUTDOWN_TIMEOUT,
            identity=identity,
        )
        async with worker:
            await tracker.wait_until_idle(debounce)
            log.info(
                "lifetime %d: idle for %gs (%d activities ran); draining",
                lifetime,
                debounce,
                tracker.finished,
            )
    log.info("lifetime %d: drained", lifetime)


def create_app(run: RunWorker = run_worker) -> tuple[BedrockAgentCoreApp, AgentCoreWorkerHost]:
    """A ``BedrockAgentCoreApp`` whose entrypoint starts Worker lifetimes via ``run``."""
    agentcore_app = BedrockAgentCoreApp()
    host = AgentCoreWorkerHost(agentcore_app, run)

    @agentcore_app.entrypoint
    async def invoke(payload: dict[str, Any]) -> dict[str, Any]:
        """Start the Worker and acknowledge. The payload asks for capacity, not work."""
        return await host.invoke(payload)

    return agentcore_app, host


app, host = create_app()


def main(agentcore_app: BedrockAgentCoreApp | None = None) -> None:
    """Serve ``/invocations`` and ``/ping`` (default 127.0.0.1:8080; AGENTCORE_HOST/PORT)."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    port = int(os.environ.get("AGENTCORE_PORT", "8080"))
    host_ip = os.environ.get("AGENTCORE_HOST") or None
    log.info(
        "AgentCore app: deployment=%s build=%s queue=%s model=%s debounce=%gs",
        config.deployment_name(),
        os.environ.get("TEMPORAL_BUILD_ID", "<BUILD_ID file>"),
        config.versioned_task_queue(),
        config.model_id(),
        config.debounce_seconds(),
    )
    (agentcore_app or app).run(port=port, host=host_ip)


if __name__ == "__main__":
    main()
