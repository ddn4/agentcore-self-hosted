"""StackSnapshot from fake pollers, ports, pidfiles and processes (DESIGN.md 5.1, 4.4)."""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import pytest

from demo_console.events import EventBus
from demo_console.fakes import FakeGateway, FakeHttp, FakeRunner, make_settings
from demo_console.payloads import iso, now
from demo_console.procs import ProcessManager, ProcRecord
from demo_console.stack import StackProbe, compute_kind, drop_drained_lifetimes, fresh

pytestmark = pytest.mark.unit
HOST = socket.gethostname()


def probe(
    tmp_path: Path,
    gateway: FakeGateway,
    runner: FakeRunner,
    http: FakeHttp,
    profile: str = "local",
    **env: str,
) -> StackProbe:
    settings = make_settings(tmp_path, profile=profile, **env)
    procs = ProcessManager(settings, runner, EventBus())

    async def detector() -> Any:
        from demo_console.detect import Detection

        return Detection(None, "test", None)

    return StackProbe(settings, gateway, procs, runner, http, detector=detector)


def comp(snap: dict[str, Any], cid: str) -> dict[str, Any]:
    return next(c for c in snap["components"] if c["id"] == cid)


def poller(identity: str) -> dict[str, Any]:
    return {"identity": identity, "last_access": iso(now())}


async def test_temporal_down(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp
) -> None:
    gateway.up = False
    snap = await probe(tmp_path, gateway, runner, http).snapshot()
    assert comp(snap, "temporal")["status"] == "down"
    # The page adds the fix (a Start button locally, the pf command elsewhere).
    assert comp(snap, "temporal")["detail"] == "Temporal is not reachable at 127.0.0.1:7233"
    assert comp(snap, "endpoint")["status"] == "unknown"
    assert snap["shape"] == "unknown" and snap["port_forward"] is None
    assert snap["temporal_error"] == "connection refused"


async def test_local_plain(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp
) -> None:
    gateway.pollers_by_queue = {
        "agents/support-dev-tq": [poller("9001@elsewhere")],
        "agent-services/agent-services-tq": [poller("9002@elsewhere")],
    }
    gateway.endpoint = {
        "name": "finance-agent",
        "namespace": "agent-services",
        "task_queue": "agent-services-tq",
    }
    http.ports = {8233, 8081}
    http.get_answers = {
        "http://127.0.0.1:8081/healthz": (200, {"status": "ok"}),
        "http://127.0.0.1:8081/refunds": (200, [{}, {}, {}, {}]),
    }
    p = probe(tmp_path, gateway, runner, http, TEMPORAL_BUILD_ID="4ee7f87")
    snap = await p.snapshot()
    assert snap["shape"] == "plain"
    assert snap["build_id"] == {"value": "4ee7f87", "source": "env"}
    assert snap["model"] == {"mode": "unknown", "source": "unknown"}
    assert comp(snap, "temporal")["detail"] == "127.0.0.1:7233, server 1.32.0"
    assert comp(snap, "endpoint")["detail"] == "finance-agent → agent-services/agent-services-tq"
    assert comp(snap, "finance")["status"] == "external" and comp(snap, "finance")["count"] == 1
    assert (
        comp(snap, "support")["status"] == "up"
        and comp(snap, "support")["detail"] == "1 on support-dev-tq"
    )
    assert (
        comp(snap, "payments")["detail"] == "4 refunds"
        and comp(snap, "payments")["status"] == "external"
    )
    assert comp(snap, "agentcore")["status"] == "down"
    assert set(snap["pollers"]) == {
        "agents/support-dev-tq",
        "agents/support-tq",
        "agent-services/agent-services-tq",
        "agents/nexus-smoke-tq",
    }
    assert p.facts.support_dev_pollers == 1 and p.facts.payments_up and p.facts.endpoint_ok


async def test_serverless_wci_and_zero(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp
) -> None:
    (tmp_path / ".temporal").mkdir(exist_ok=True)
    (tmp_path / ".temporal" / "dev-server.pid").write_text("777")
    (tmp_path / ".temporal" / "dev-server.wci").write_text("1\n")
    runner.alive_pids.add(777)
    gateway.deployment_info = {
        "name": "support-supervisor",
        "current_build": "4ee7f87",
        "versions": ["4ee7f87"],
    }
    gateway.version_info = {
        "providers": [
            {"type": "aws-agentcore", "details": {"endpoint": "runtime/support_supervisor_local/x"}}
        ]
    }
    http.ports = {8080, 4599, 8233}
    http.get_answers = {"http://127.0.0.1:8080/ping": (200, {"status": "Healthy"})}
    p = probe(tmp_path, gateway, runner, http)
    snap = await p.snapshot()
    assert snap["build_id"] == {"value": "4ee7f87", "source": "deployment"}
    assert (
        snap["deployment"]["compute"] == "local-wci"
        and snap["deployment"]["current_build"] == "4ee7f87"
    )
    assert snap["wci"]["mode"] == "emulator"
    assert snap["shape"] == "serverless-wci"
    support = comp(snap, "support")
    assert support["status"] == "zero" and support["detail"] == "0 on support-tq"
    assert comp(snap, "agentcore")["detail"] == "Healthy (idle)"
    assert comp(snap, "emulator")["status"] == "external"
    assert comp(snap, "temporal")["status"] == "external"  # dev server not started by the console


async def test_counters_belong_to_this_dev_server(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp
) -> None:
    """After dev-server-reset the old emulator JSONL and app log describe a deleted database."""
    import json
    import os

    t = tmp_path / ".temporal"
    t.mkdir(exist_ok=True)
    pid = t / "dev-server.pid"
    pid.write_text("777")
    runner.alive_pids.add(777)
    started = 1_800_000_000.0
    os.utime(pid, (started, started))
    calls = [
        {"op": "InvokeAgentRuntime", "time": "2027-01-15T07:00:00Z", "answer": {"lifetime": 4}},
        {"op": "InvokeAgentRuntime", "time": "2027-01-15T08:20:00Z", "answer": {"lifetime": 5}},
    ]
    (t / "agentcore-emulator.jsonl").write_text("".join(json.dumps(c) + "\n" for c in calls))
    logs = tmp_path / ".temporal" / "console" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "agentcore-local.log").write_text(
        "2027-01-15 08:00:00,000 lifetime 5: connected to 127.0.0.1:7233\n"
    )
    p = probe(tmp_path, gateway, runner, http)
    snap = await p.snapshot()
    # 1_800_000_000 is 2027-01-15T08:00:00Z: only the second invocation is this server's.
    assert snap["wci"]["invokes_total"] == 1
    # The app is down: its old log's lifetime number means nothing now.
    assert snap["agentcore_app"] == {
        "status": "down",
        "lifetime": None,
        "url": "http://127.0.0.1:8080",
    }


def test_compute_kind() -> None:
    assert compute_kind(None) == "none"
    assert compute_kind({"providers": []}) == "none"
    assert (
        compute_kind({"providers": [{"type": "aws-agentcore", "details": "runtime/x" + "0" * 12}]})
        == "local-wci"
    )
    assert (
        compute_kind(
            {"providers": [{"type": "aws-agentcore", "details": {"arn": "runtime/AgentcoreDemo"}}]}
        )
        == "agentcore-aws"
    )


async def test_build_id_order(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp
) -> None:
    p = probe(tmp_path, gateway, runner, http)
    # No env, no deployment: build-id.sh refuses on a dirty uv.lock -> none.
    runner.result_for("scripts/build-id.sh", 1, "build-id: refusing: uv.lock is uncommitted")
    assert (await p.resolve_build_id(None)).value is None
    p._script_build = None
    runner.result_for("scripts/build-id.sh", 0, "4ee7f87\n")
    found = await p.resolve_build_id(None)
    assert (found.value, found.source) == ("4ee7f87", "script")
    found = await p.resolve_build_id("abc1234")
    assert (found.value, found.source) == ("abc1234", "deployment")
    p.settings.base_env["TEMPORAL_BUILD_ID"] = "fffffff"
    found = await p.resolve_build_id("abc1234")
    assert (found.value, found.source) == ("fffffff", "env")


def test_stale_local_pollers_are_dropped() -> None:
    pollers = [poller(f"123@{HOST}"), poller(f"456@{HOST}/agentcore-lifetime-1"), poller("789@pod")]
    alive = {456}
    kept = fresh(pollers, host=HOST, pid_alive=lambda pid: pid in alive)
    assert [p["identity"] for p in kept] == [f"456@{HOST}/agentcore-lifetime-1", "789@pod"]
    old = {"identity": "1@pod", "last_access": "2020-01-01T00:00:00.000Z"}
    assert fresh([old]) == []


async def test_model_mode_from_managed_variants(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp
) -> None:
    p = probe(tmp_path, gateway, runner, http)
    for name, variant, pid in (
        ("worker-finance", "scripted", 11),
        ("worker-support", "scripted", 12),
    ):
        runner.alive_groups.add(pid)
        p.procs.records[name] = ProcRecord(name, pid, pid, "t", variant, "local", "now")
    assert (await p.snapshot())["model"] == {"mode": "scripted", "source": "managed"}
    p.procs.records["worker-support"].variant = "live"
    assert (await p.snapshot())["model"]["mode"] == "mixed"


async def test_eks_profile(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp
) -> None:
    gateway.deployment_info = {
        "name": "support-supervisor",
        "current_build": "4ee7f87",
        "versions": [],
    }
    gateway.version_info = {
        "providers": [
            {
                "type": "aws-agentcore",
                "details": {"arn": "runtime/AgentcoreDemo_default_support_supervisor"},
            }
        ]
    }
    p = probe(tmp_path, gateway, runner, http, profile="eks-pf")
    snap = await p.snapshot()
    assert snap["shape"] == "agentcore-aws" and snap["profile"]["tls"] is True
    assert comp(snap, "agentcore-aws") == {
        "id": "agentcore-aws",
        "label": "AgentCore (AWS)",
        "status": "unknown",
        "detail": "not checked (AWS)",
        "group": "support",
    }
    assert "agentcore" not in {c["id"] for c in snap["components"]}
    assert "payments" not in {c["id"] for c in snap["components"]}
    assert snap["wci"]["mode"] == "cluster"


def test_drained_lifetimes_leave_the_support_count_at_once() -> None:
    lifetime = {"identity": "4711@laptop/agentcore-lifetime-2", "last_access": iso(now())}
    other_host = {"identity": "9@microvm/agentcore-lifetime-1", "last_access": iso(now())}
    plain = poller("4712@laptop")
    pollers = [lifetime, other_host, plain]
    assert drop_drained_lifetimes(pollers, "HealthyBusy", "laptop") == pollers
    assert drop_drained_lifetimes(pollers, None, "laptop") == pollers
    assert drop_drained_lifetimes(pollers, "Healthy", "laptop") == [other_host, plain]


def test_only_the_running_lifetime_counts_while_busy() -> None:
    old = {"identity": "4711@laptop/agentcore-lifetime-4", "last_access": iso(now())}
    new = {"identity": "4711@laptop/agentcore-lifetime-5", "last_access": iso(now())}
    assert drop_drained_lifetimes([old, new], "HealthyBusy", "laptop", running=5) == [new]
    # An app started outside the console has no log here: keep both (the server's minute).
    assert drop_drained_lifetimes([old, new], "HealthyBusy", "laptop") == [old, new]
