"""The console's only way to Temporal (DESIGN.md 4.2, 4.3).

``Clients`` connects lazily (1 s -> 10 s back-off) with the same envconfig as make and offers
exactly the calls the console makes. Namespace ``agents`` goes through ``AgentsClient``, which
has no query path at all: **ledger 72**, a query on a Support Workflow on ``support-tq`` makes
the WCI start an AgentCore session (a paid one on EKS) and hangs when nothing polls. Support's
state comes from ``fetch_history`` and ``describe`` only. Queries are allowed in
``agent-services`` (Finance's ``get_task``), always with a short RPC timeout.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

import temporalio.api.enums.v1 as enums
import temporalio.api.operatorservice.v1 as opsv1
import temporalio.api.workflowservice.v1 as wsv1
from temporalio.api.deployment.v1 import WorkerDeploymentVersion
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.client import Client, WorkflowHistory
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.envconfig import ClientConfig
from temporalio.service import RPCError, RPCStatusCode

from demo_console.payloads import decode, iso, ts
from demo_console.settings import AGENTS_NS, SERVICES_NS, Settings

RPC_TIMEOUT = timedelta(seconds=5)


class TemporalUnreachable(RuntimeError):
    """Temporal did not answer (the stack bar shows how to start it)."""


class SupportQueryForbidden(RuntimeError):
    """Ledger 72: the console never queries a Workflow in namespace ``agents``."""


class NotFound(LookupError):
    """No such Workflow (or endpoint, deployment...)."""


@dataclass(frozen=True)
class Execution:
    """One row of a visibility list."""

    workflow_id: str
    run_id: str
    workflow_type: str
    status: str
    task_queue: str
    start_time: str | None
    close_time: str | None
    build_id: str | None = None


class Gateway(Protocol):
    """What the rest of the console needs from Temporal (fakes implement it in tests)."""

    @property
    def connected(self) -> bool: ...

    @property
    def last_error(self) -> str | None: ...

    async def ensure(self) -> bool: ...

    async def close(self) -> None: ...

    async def system_info(self) -> dict[str, Any]: ...

    async def fetch_history(
        self, namespace: str, workflow_id: str, run_id: str | None = None
    ) -> WorkflowHistory: ...

    async def describe(
        self, namespace: str, workflow_id: str, run_id: str | None = None
    ) -> wsv1.DescribeWorkflowExecutionResponse: ...

    async def list_workflows(
        self, namespace: str, query: str, limit: int = 25
    ) -> list[Execution]: ...

    async def query(
        self, namespace: str, workflow_id: str, name: str, within: float = 5.0
    ) -> Any: ...

    async def signal(self, namespace: str, workflow_id: str, name: str, arg: Any) -> None: ...

    async def start_workflow(
        self,
        namespace: str,
        workflow_type: str,
        args: list[Any],
        *,
        workflow_id: str,
        task_queue: str,
        execution_timeout: timedelta | None = None,
    ) -> tuple[str, bool]: ...

    async def terminate(self, namespace: str, workflow_id: str, reason: str) -> None: ...

    async def pollers(self, namespace: str, task_queue: str) -> list[dict[str, Any]]: ...

    async def nexus_endpoint(self, name: str) -> dict[str, Any] | None: ...

    async def deployment(self, name: str) -> dict[str, Any] | None: ...

    async def deployment_version(self, name: str, build_id: str) -> dict[str, Any] | None: ...


class AgentsClient:
    """The SDK client for ``agents`` without a query path (ledger 72)."""

    def __init__(self, client: Client) -> None:
        self._client = client

    @property
    def client(self) -> Client:
        return self._client

    async def fetch_history(self, workflow_id: str, run_id: str | None) -> WorkflowHistory:
        return await self._client.get_workflow_handle(workflow_id, run_id=run_id).fetch_history(
            rpc_timeout=timedelta(seconds=15)
        )

    async def describe(
        self, workflow_id: str, run_id: str | None
    ) -> wsv1.DescribeWorkflowExecutionResponse:
        handle = self._client.get_workflow_handle(workflow_id, run_id=run_id)
        return (await handle.describe(rpc_timeout=RPC_TIMEOUT)).raw_description

    async def query(self, *_args: Any, **_kwargs: Any) -> Any:
        raise SupportQueryForbidden(
            "the console never queries Support Workflows (ledger 72: a query wakes AgentCore)"
        )


def _status(value: int) -> str:
    name = enums.WorkflowExecutionStatus.Name(value)  # type: ignore[arg-type]
    return name.removeprefix("WORKFLOW_EXECUTION_STATUS_")


def _unreachable(err: RPCError) -> bool:
    return err.status in (RPCStatusCode.UNAVAILABLE, RPCStatusCode.DEADLINE_EXCEEDED)


class Clients:
    """Lazy SDK clients for ``agents`` and ``agent-services``; implements ``Gateway``."""

    def __init__(
        self,
        settings: Settings,
        *,
        agents: Client | None = None,
        services: Client | None = None,
    ) -> None:
        self.settings = settings
        self._agents = AgentsClient(agents) if agents else None
        self._services = services
        self._injected = agents is not None
        self._error: str | None = None
        self._next_try = 0.0
        self._backoff = 1.0
        self._lock = asyncio.Lock()

    @classmethod
    def from_clients(cls, settings: Settings, agents: Client, services: Client) -> Clients:
        """Use existing clients (integration tests); they are never reconnected."""
        return cls(settings, agents=agents, services=services)

    # --- connection ----------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._agents is not None and self._services is not None

    @property
    def last_error(self) -> str | None:
        return self._error

    def connect_config(self) -> dict[str, Any]:
        cfg = ClientConfig.load_client_connect_config(
            profile=self.settings.profile,
            config_file=str(self.settings.config_file),
            override_env_vars=self.settings.temporal_env(),
        )
        # Cert paths in config/temporal.toml are relative to the repo root: __main__ chdirs there.
        cfg.setdefault("target_host", "127.0.0.1:7233")
        return dict(cfg)

    async def ensure(self) -> bool:
        if self.connected:
            return True
        if self._injected:
            return False
        async with self._lock:
            if self.connected:
                return True
            if time.monotonic() < self._next_try:
                return False
            try:
                cfg = self.connect_config()
                async with asyncio.timeout(5):
                    agents = await Client.connect(**{**cfg, "namespace": AGENTS_NS})
                    services = await Client.connect(**{**cfg, "namespace": SERVICES_NS})
            except Exception as err:  # unreachable, TLS failure, bad config
                self._error = f"{type(err).__name__}: {err}" if str(err) else type(err).__name__
                self._next_try = time.monotonic() + self._backoff
                self._backoff = min(self._backoff * 2, 10.0)
                return False
            self._agents, self._services = AgentsClient(agents), services
            self._error = None
            self._backoff = 1.0
            return True

    async def close(self) -> None:
        if not self._injected:
            self._agents = None
            self._services = None
        self._next_try = 0.0

    def _drop(self, err: BaseException) -> None:
        self._error = str(err) or type(err).__name__
        if not self._injected:
            self._agents = None
            self._services = None

    async def _a(self) -> AgentsClient:
        if not await self.ensure() or self._agents is None:
            raise TemporalUnreachable(self._error or "not connected")
        return self._agents

    async def _s(self) -> Client:
        if not await self.ensure() or self._services is None:
            raise TemporalUnreachable(self._error or "not connected")
        return self._services

    async def _client(self, namespace: str) -> Client:
        if namespace == AGENTS_NS:
            return (await self._a()).client
        if namespace == SERVICES_NS:
            return await self._s()
        raise ValueError(f"unknown namespace {namespace!r}")

    async def _call(self, coro: Any) -> Any:
        try:
            return await coro
        except RPCError as err:
            if err.status == RPCStatusCode.NOT_FOUND:
                raise NotFound(str(err)) from err
            if _unreachable(err):
                self._drop(err)
                raise TemporalUnreachable(str(err)) from err
            raise

    # --- reads ---------------------------------------------------------------------------

    async def system_info(self) -> dict[str, Any]:
        client = await self._client(AGENTS_NS)
        resp = await self._call(
            client.workflow_service.get_system_info(
                wsv1.GetSystemInfoRequest(), timeout=RPC_TIMEOUT
            )
        )
        return {"server_version": resp.server_version}

    async def fetch_history(
        self, namespace: str, workflow_id: str, run_id: str | None = None
    ) -> WorkflowHistory:
        if namespace == AGENTS_NS:
            agents = await self._a()
            return await self._call(agents.fetch_history(workflow_id, run_id))
        client = await self._client(namespace)
        handle = client.get_workflow_handle(workflow_id, run_id=run_id)
        return await self._call(handle.fetch_history(rpc_timeout=timedelta(seconds=15)))

    async def describe(
        self, namespace: str, workflow_id: str, run_id: str | None = None
    ) -> wsv1.DescribeWorkflowExecutionResponse:
        if namespace == AGENTS_NS:
            agents = await self._a()
            return await self._call(agents.describe(workflow_id, run_id))
        client = await self._client(namespace)
        handle = client.get_workflow_handle(workflow_id, run_id=run_id)
        return (await self._call(handle.describe(rpc_timeout=RPC_TIMEOUT))).raw_description

    async def list_workflows(self, namespace: str, query: str, limit: int = 25) -> list[Execution]:
        client = await self._client(namespace)
        out: list[Execution] = []

        async def collect() -> None:
            async for ex in client.list_workflows(
                query, limit=limit, page_size=min(limit, 100), rpc_timeout=RPC_TIMEOUT
            ):
                raw = ex.raw_info
                out.append(
                    Execution(
                        workflow_id=ex.id,
                        run_id=ex.run_id,
                        workflow_type=ex.workflow_type,
                        status=_status(raw.status),
                        task_queue=ex.task_queue,
                        start_time=iso(ts(raw.start_time)),
                        close_time=iso(ts(raw.close_time)),
                        build_id=raw.versioning_info.deployment_version.build_id or None,
                    )
                )

        await self._call(collect())
        return out

    async def query(self, namespace: str, workflow_id: str, name: str, within: float = 5.0) -> Any:
        if namespace == AGENTS_NS:
            return await (await self._a()).query(workflow_id, name)
        client = await self._client(namespace)
        handle = client.get_workflow_handle(workflow_id)
        return await self._call(handle.query(name, rpc_timeout=timedelta(seconds=within)))

    async def pollers(self, namespace: str, task_queue: str) -> list[dict[str, Any]]:
        client = await self._client(namespace)
        kinds = [enums.TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW]
        kinds.append(enums.TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY)
        if namespace == SERVICES_NS:
            kinds.append(enums.TaskQueueType.TASK_QUEUE_TYPE_NEXUS)
        seen: dict[str, dict[str, Any]] = {}
        for kind in kinds:
            resp = await self._call(
                client.workflow_service.describe_task_queue(
                    wsv1.DescribeTaskQueueRequest(
                        namespace=namespace,
                        task_queue=TaskQueue(name=task_queue),
                        task_queue_type=kind,
                    ),
                    timeout=RPC_TIMEOUT,
                )
            )
            for poller in resp.pollers:
                when = ts(poller.last_access_time)
                entry = seen.setdefault(
                    poller.identity, {"identity": poller.identity, "last_access": None}
                )
                if when and (entry["last_access"] is None or iso(when) > entry["last_access"]):
                    entry["last_access"] = iso(when)
                bid = poller.deployment_options.build_id
                if bid:
                    entry["build_id"] = bid
        return list(seen.values())

    async def nexus_endpoint(self, name: str) -> dict[str, Any] | None:
        client = await self._client(AGENTS_NS)
        resp = await self._call(
            client.operator_service.list_nexus_endpoints(
                opsv1.ListNexusEndpointsRequest(name=name), timeout=RPC_TIMEOUT
            )
        )
        for ep in resp.endpoints:
            if ep.spec.name == name:
                worker = ep.spec.target.worker
                return {
                    "name": name,
                    "namespace": worker.namespace,
                    "task_queue": worker.task_queue,
                }
        return None

    async def deployment(self, name: str) -> dict[str, Any] | None:
        client = await self._client(AGENTS_NS)
        try:
            resp = await self._call(
                client.workflow_service.describe_worker_deployment(
                    wsv1.DescribeWorkerDeploymentRequest(namespace=AGENTS_NS, deployment_name=name),
                    timeout=RPC_TIMEOUT,
                )
            )
        except NotFound:
            return None
        info = resp.worker_deployment_info
        current = info.routing_config.current_deployment_version.build_id or None
        return {
            "name": name,
            "current_build": current,
            "versions": [v.deployment_version.build_id for v in info.version_summaries],
        }

    async def deployment_version(self, name: str, build_id: str) -> dict[str, Any] | None:
        client = await self._client(AGENTS_NS)
        try:
            resp = await self._call(
                client.workflow_service.describe_worker_deployment_version(
                    wsv1.DescribeWorkerDeploymentVersionRequest(
                        namespace=AGENTS_NS,
                        deployment_version=WorkerDeploymentVersion(
                            deployment_name=name, build_id=build_id
                        ),
                    ),
                    timeout=RPC_TIMEOUT,
                )
            )
        except NotFound:
            return None
        providers: list[dict[str, Any]] = []
        groups = resp.worker_deployment_version_info.compute_config.scaling_groups
        for group in groups.values():
            details = decode(group.provider.details)
            providers.append({"type": group.provider.type, "details": details})
        return {"name": name, "build_id": build_id, "providers": providers}

    # --- writes --------------------------------------------------------------------------

    async def signal(self, namespace: str, workflow_id: str, name: str, arg: Any) -> None:
        if namespace == AGENTS_NS:
            raise ValueError("the console only signals Finance tasks (agent-services)")
        client = await self._client(namespace)
        handle = client.get_workflow_handle(workflow_id)
        await self._call(handle.signal(name, arg, rpc_timeout=RPC_TIMEOUT))

    async def start_workflow(
        self,
        namespace: str,
        workflow_type: str,
        args: list[Any],
        *,
        workflow_id: str,
        task_queue: str,
        execution_timeout: timedelta | None = None,
    ) -> tuple[str, bool]:
        """Start (or attach to the open run with the same ID); ``(run_id, attached)``."""
        client = await self._client(namespace)
        try:
            desc = await self.describe(namespace, workflow_id)
            info = desc.workflow_execution_info
            if info.status == enums.WorkflowExecutionStatus.WORKFLOW_EXECUTION_STATUS_RUNNING:
                return info.execution.run_id, True
        except NotFound:
            pass
        handle = await self._call(
            client.start_workflow(
                workflow_type,
                args=args,
                id=workflow_id,
                task_queue=task_queue,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                execution_timeout=execution_timeout,
                rpc_timeout=RPC_TIMEOUT,
            )
        )
        return handle.result_run_id or handle.first_execution_run_id or "", False

    async def terminate(self, namespace: str, workflow_id: str, reason: str) -> None:
        client = await self._client(namespace)
        handle = client.get_workflow_handle(workflow_id)
        await self._call(handle.terminate(reason=reason, rpc_timeout=RPC_TIMEOUT))
