"""ProcessManager (DESIGN.md 4.5) with FakeRunner: nothing is really started."""

from __future__ import annotations

import asyncio
import json
import signal
from pathlib import Path

import pytest

from demo_console.events import EventBus
from demo_console.fakes import FakeRunner, make_settings
from demo_console.procs import ProcessError, ProcessManager

pytestmark = pytest.mark.unit


def manager(
    tmp_path: Path, runner: FakeRunner, profile: str = "local", build: str | None = "4ee7f87"
) -> ProcessManager:
    async def build_id() -> str | None:
        return build

    procs = ProcessManager(
        make_settings(tmp_path, profile=profile), runner, EventBus(), build_id_provider=build_id
    )
    procs.build_id_hint, procs.build_id_known = build, True
    return procs


async def test_start_spawns_make_in_its_own_session(tmp_path: Path, runner: FakeRunner) -> None:
    procs = manager(tmp_path, runner)
    body = await procs.start("worker-support")
    argv, env, log = runner.spawned[0]
    assert argv == ["make", "-s", "worker-support-scripted"]
    assert (
        env["TEMPORAL_PROFILE"] == "local"
        and log == tmp_path / ".temporal/console/logs/worker-support.log"
    )
    assert body["status"] == "running" and body["managed"] is True and body["variant"] == "scripted"
    assert body["make"] == "make worker-support-scripted" and body["cost"] == "free"
    record = json.loads((tmp_path / ".temporal/console/procs/worker-support.json").read_text())
    assert record["pid"] == record["pgid"] == body["pid"]
    assert record["target"] == "worker-support-scripted" and record["profile"] == "local"
    with pytest.raises(ProcessError) as err:
        await procs.start("worker-support")
    assert err.value.code == "precondition_failed"


async def test_build_id_goes_to_the_child(tmp_path: Path, runner: FakeRunner) -> None:
    procs = manager(tmp_path, runner)
    await procs.start("agentcore-local")
    argv, env, _ = runner.spawned[0]
    assert (
        argv == ["make", "-s", "agentcore-local-scripted"] and env["TEMPORAL_BUILD_ID"] == "4ee7f87"
    )
    nobuild = manager(tmp_path / "x", runner, build=None)
    with pytest.raises(ProcessError, match="No Build ID"):
        await nobuild.start("agentcore-local")


async def test_live_variant_needs_confirm(tmp_path: Path, runner: FakeRunner) -> None:
    procs = manager(tmp_path, runner)
    with pytest.raises(ProcessError) as err:
        await procs.start("worker-finance", variant="live")
    assert err.value.code == "confirm_required"
    body = await procs.start("worker-finance", variant="live", confirm=True)
    assert body["cost"] == "tokens" and runner.spawned[-1][0] == ["make", "-s", "worker-finance"]
    with pytest.raises(ProcessError) as err:
        await procs.start("payments-mock", variant="live")
    assert err.value.code == "invalid_param"


async def test_stop_term_then_kill(
    tmp_path: Path, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("demo_console.procs.STOP_GRACE_S", 0.3)
    procs = manager(tmp_path, runner)
    body = await procs.start("payments-mock")
    pid = body["pid"]
    runner.ignore_term.add(pid)
    stopped = await procs.stop("payments-mock")
    assert runner.signals == [(pid, signal.SIGTERM), (pid, signal.SIGKILL)]
    assert stopped["status"] == "stopped" and stopped["managed"] is False
    assert stopped["exit_code"] is not None
    assert not (tmp_path / ".temporal/console/procs/payments-mock.json").exists()


async def test_crash_demo_kill_is_recorded(tmp_path: Path, runner: FakeRunner) -> None:
    procs = manager(tmp_path, runner)
    pid = (await procs.start("worker-support"))["pid"]
    await procs.stop("worker-support", sig="KILL")
    assert runner.signals == [(pid, signal.SIGKILL)]
    kills = procs.kills()
    assert kills[0]["name"] == "worker-support" and kills[0]["pid"] == pid


async def test_exit_is_noticed_and_alerted(tmp_path: Path, runner: FakeRunner) -> None:
    procs = manager(tmp_path, runner)
    with procs.bus.subscribe() as queue:
        pid = (await procs.start("worker-smoke"))["pid"]
        runner.exit(pid, 2)
        await asyncio.sleep(0.05)
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
    assert procs.describe("worker-smoke")["status"] == "exited"
    assert procs.describe("worker-smoke")["exit_code"] == 2
    alerts = [d for e, d in events if e == "alert"]
    assert alerts and "exited with code 2" in alerts[0]["message"]


async def test_adoption_after_restart(tmp_path: Path, runner: FakeRunner) -> None:
    first = manager(tmp_path, runner)
    alive = (await first.start("payments-mock"))["pid"]
    dead = (await first.start("worker-finance"))["pid"]
    runner.alive_groups.discard(dead)  # died while the console was down (no waiter now)
    second = manager(tmp_path, runner)
    assert second.adopt() == ["payments-mock"]
    assert second.describe("payments-mock")["status"] == "running"
    assert second.describe("payments-mock")["pid"] == alive
    assert not (tmp_path / ".temporal/console/procs/worker-finance.json").exists()


async def test_external_detection(tmp_path: Path, runner: FakeRunner) -> None:
    procs = manager(tmp_path, runner)
    procs.ports_open = {8081}
    procs.polled = {"agents/support-dev-tq"}
    assert procs.describe("payments-mock")["status"] == "external"
    assert procs.describe("worker-support")["status"] == "external"
    with pytest.raises(ProcessError, match="outside the console"):
        await procs.stop("worker-support")
    with pytest.raises(ProcessError, match="already running"):
        await procs.start("payments-mock")


async def test_profiles_and_certs(tmp_path: Path, runner: FakeRunner) -> None:
    kind = manager(tmp_path, runner, profile="kind")
    assert kind.describe("payments-mock")["status"] == "unavailable"
    why = kind.describe("worker-support")["why"]
    assert why == "No client certificate in .certs/laptop-support/ (make certs-pull)."
    cert = tmp_path / ".certs/laptop-support"
    cert.mkdir(parents=True)
    (cert / "tls.crt").write_text("x")
    assert kind.describe("worker-support")["status"] == "stopped"
    eks = manager(tmp_path, runner, profile="eks-pf")
    assert "certs-pull TEMPORAL_PROFILE=eks-pf" in eks.describe("agentcore-local")["why"]


class DevServerRunner(FakeRunner):
    """``make dev-server-up`` writes the pidfile, as scripts/dev-server.sh does."""

    def __init__(self, temporal_dir: Path) -> None:
        super().__init__()
        self.temporal_dir = temporal_dir

    async def run(self, argv, *, env, cwd, within=60.0):
        if argv[:3] == ["make", "-s", "dev-server-up"]:
            (self.temporal_dir / "dev-server.pid").write_text("555")
            (self.temporal_dir / "dev-server.wci").write_text(env["WCI"])
            self.alive_pids.add(555)
        return await super().run(argv, env=env, cwd=cwd, within=within)


async def test_dev_server_via_script(tmp_path: Path) -> None:
    runner = DevServerRunner(tmp_path / ".temporal")
    procs = manager(tmp_path, runner)
    with pytest.raises(ProcessError, match="never aws"):
        await procs.start("dev-server", wci=2)
    body = await procs.start("dev-server", wci=1)
    argv, env = runner.ran[-1]
    assert argv == ["make", "-s", "dev-server-up", "WCI=1"] and env["WCI"] == "1"
    assert body["status"] == "running" and body["wci"] == "1"
    assert body["make"] == "make dev-server-up WCI=1" and body["pid"] == 555
    with pytest.raises(ProcessError, match="already running"):
        await procs.start("dev-server")
    await procs.stop("dev-server")
    assert runner.ran[-1][0] == ["make", "-s", "dev-server-down"]


async def test_other_profile_blocks_a_switch(tmp_path: Path, runner: FakeRunner) -> None:
    procs = manager(tmp_path, runner)
    await procs.start("worker-support")
    assert procs.other_profile("kind") == ["worker-support"]
    assert procs.other_profile("local") == []
