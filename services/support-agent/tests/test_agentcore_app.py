"""The AgentCore entrypoint's contract, with a fake ``BedrockAgentCoreApp`` (v0.7).

Plan v0.7 acceptance: a second invocation does not start a second Worker, and
``complete_async_task`` runs on normal exit, on an exception and on a connect timeout.
Also: the acknowledgement is immediate, a WCI payload for another version is refused,
and a missing Build ID refuses before any async task is registered.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.runtime.models import PingStatus
from temporalio.client import Client, Plugin

from support_agent import agentcore_app, config
from support_agent.agentcore_app import AgentCoreWorkerHost, WorkerVersion

pytestmark = pytest.mark.unit


class FakeApp:
    """Records add/complete calls like BedrockAgentCoreApp's async-task tracking."""

    def __init__(self) -> None:
        self.active: dict[int, dict[str, Any]] = {}
        self.added: list[int] = []
        self.completed: list[int] = []
        self._next = 0

    def add_async_task(self, name: str, metadata: dict[str, Any] | None = None) -> int:
        self._next += 1
        self.active[self._next] = {"name": name, **(metadata or {})}
        self.added.append(self._next)
        return self._next

    def complete_async_task(self, task_id: int) -> bool:
        self.completed.append(task_id)
        return self.active.pop(task_id, None) is not None


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEMPORAL_BUILD_ID", "abc1234")
    monkeypatch.delenv("TEMPORAL_DEPLOYMENT_NAME", raising=False)
    monkeypatch.delenv("TEMPORAL_TASK_QUEUE", raising=False)


async def test_ack_is_immediate_and_names_the_version() -> None:
    app = FakeApp()
    release = asyncio.Event()

    async def run(version: WorkerVersion, lifetime: int) -> None:
        await release.wait()

    host = AgentCoreWorkerHost(app, run)
    ack = await asyncio.wait_for(host.invoke({}), timeout=0.5)
    assert ack == {
        "message": "worker starting",
        "task_queue": "support-tq",
        "deployment_name": "support-supervisor",
        "build_id": "abc1234",
        "lifetime": 1,
    }
    assert app.active[1] == {"name": "temporal-worker", "build_id": "abc1234", "lifetime": 1}
    release.set()
    await host.wait()
    assert app.completed == [1] and not app.active


async def test_second_invocation_does_not_start_a_second_worker() -> None:
    app = FakeApp()
    started: list[int] = []
    release = asyncio.Event()

    async def run(version: WorkerVersion, lifetime: int) -> None:
        started.append(lifetime)
        await release.wait()

    host = AgentCoreWorkerHost(app, run)
    await host.invoke({"deploymentName": "support-supervisor", "buildId": "abc1234"})
    await asyncio.sleep(0)
    again = await host.invoke({"deploymentName": "support-supervisor", "buildId": "abc1234"})
    assert again["message"] == "worker already polling"
    assert started == [1] and app.added == [1]
    release.set()
    await host.wait()
    # After the drain, the next invocation starts the next lifetime (Case B's session #2).
    third = await host.invoke({})
    assert third["message"] == "worker starting" and third["lifetime"] == 2
    await host.wait()
    assert started == [1, 2] and app.completed == [1, 2]


async def test_complete_async_task_on_exception(caplog: pytest.LogCaptureFixture) -> None:
    app = FakeApp()

    async def run(version: WorkerVersion, lifetime: int) -> None:
        raise RuntimeError("worker crashed")

    host = AgentCoreWorkerHost(app, run)
    with caplog.at_level(logging.ERROR, logger="support_agent.agentcore"):
        await host.invoke({})
        await host.wait()
    assert app.completed == [1] and not app.active
    assert "worker lifetime 1 failed" in caplog.text
    assert not host.polling


async def test_complete_async_task_on_connect_timeout(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The real ``run_worker`` with a frontend that never answers: fail fast, release."""
    monkeypatch.setenv("TEMPORAL_CONNECT_TIMEOUT_SECONDS", "0.2")

    async def never_connects(plugins: list[Plugin] | None = None) -> Client:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    monkeypatch.setattr(config, "connect", never_connects)
    app = FakeApp()
    host = AgentCoreWorkerHost(app, agentcore_app.run_worker)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    with caplog.at_level(logging.ERROR, logger="support_agent.agentcore"):
        await host.invoke({})
        await asyncio.wait_for(host.wait(), timeout=3)
    assert loop.time() - t0 < 2
    assert app.completed == [1] and not app.active
    assert "could not connect to Temporal within 0.2s" in caplog.text


async def test_complete_async_task_on_cancellation() -> None:
    app = FakeApp()

    async def run(version: WorkerVersion, lifetime: int) -> None:
        await asyncio.sleep(3600)

    host = AgentCoreWorkerHost(app, run)
    await host.invoke({})
    await asyncio.sleep(0)
    assert host._worker is not None
    host._worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await host._worker
    assert app.completed == [1]


@pytest.mark.parametrize(
    "payload",
    [{"deploymentName": "support-supervisor", "buildId": "zzz9999"}, {"deploymentName": "other"}],
)
async def test_payload_for_another_version_is_refused(payload: dict[str, str]) -> None:
    app = FakeApp()

    async def run(version: WorkerVersion, lifetime: int) -> None:
        raise AssertionError("must not start")

    host = AgentCoreWorkerHost(app, run)
    ack = await host.invoke(payload)
    assert "error" in ack and not app.added


async def test_missing_build_id_refuses_before_registering_a_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEMPORAL_BUILD_ID")
    monkeypatch.setattr(config, "_build_id_files", lambda: [])
    app = FakeApp()

    async def run(version: WorkerVersion, lifetime: int) -> None:
        raise AssertionError("must not start")

    ack = await AgentCoreWorkerHost(app, run).invoke({})
    assert "TEMPORAL_BUILD_ID" in ack["error"] and not app.added


async def test_real_app_ping_is_busy_only_while_a_worker_lives() -> None:
    """With the real BedrockAgentCoreApp: /ping's status follows the async task."""
    release = asyncio.Event()

    async def run(version: WorkerVersion, lifetime: int) -> None:
        await release.wait()

    app, host = agentcore_app.create_app(run)
    assert isinstance(app, BedrockAgentCoreApp)
    assert app.get_current_ping_status() == PingStatus.HEALTHY
    await host.invoke({})
    assert app.get_current_ping_status() == PingStatus.HEALTHY_BUSY
    release.set()
    await host.wait()
    assert app.get_current_ping_status() == PingStatus.HEALTHY


def test_versioned_queue_ignores_the_dev_queue_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPPORT_TASK_QUEUE", "support-dev-tq")  # what .env sets
    assert config.versioned_task_queue() == "support-tq"
    monkeypatch.setenv("TEMPORAL_TASK_QUEUE", "support-tq-2")
    assert config.versioned_task_queue() == "support-tq-2"


def test_build_id_from_the_bundle_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.delenv("TEMPORAL_BUILD_ID")
    (tmp_path / "BUILD_ID").write_text("f00d123\n")
    monkeypatch.setattr(config, "_build_id_files", lambda: [tmp_path / "BUILD_ID"])
    assert config.build_id() == "f00d123"


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [("AGENTCORE_DEBOUNCE_SECONDS", "", 20.0), ("AGENTCORE_DEBOUNCE_SECONDS", "30", 30.0)],
)
def test_debounce_default_and_override(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str, expected: float
) -> None:
    monkeypatch.setenv(name, value)
    assert config.debounce_seconds() == expected


def test_bad_debounce_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTCORE_DEBOUNCE_SECONDS", "0")
    with pytest.raises(ValueError):
        config.debounce_seconds()


def _client_with(target: str, tls: Any) -> Any:
    """Just the attributes ``describe_connection`` reads (a real Client needs a server)."""
    from types import SimpleNamespace

    return SimpleNamespace(
        namespace="agents",
        service_client=SimpleNamespace(config=SimpleNamespace(target_host=target, tls=tls)),
    )


def test_describe_connection_mtls_names_the_server_and_no_secrets() -> None:
    """v0.12: the AgentCore log line shows the Route 53 name, mTLS on, no PEM material."""
    from temporalio.service import TLSConfig

    tls = TLSConfig(
        server_root_ca_cert=b"not-a-real-ca",
        client_cert=b"not-a-real-cert",
        client_private_key=b"not-a-real-key",
        domain="temporal-frontend.example.internal",
    )
    line = agentcore_app.describe_connection(
        _client_with("temporal-frontend.example.internal:7233", tls)
    )
    assert line == (
        "temporal-frontend.example.internal:7233 namespace agents "
        "(TLS, server name temporal-frontend.example.internal, client certificate yes)"
    )
    assert "not-a-real" not in line


def test_describe_connection_plaintext() -> None:
    line = agentcore_app.describe_connection(_client_with("127.0.0.1:7233", False))
    assert line == "127.0.0.1:7233 namespace agents (plaintext)"
