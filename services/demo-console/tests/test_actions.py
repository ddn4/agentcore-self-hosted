"""Actions, the cost gate and recipes (DESIGN.md 2.3, 4.5, 5.5): FakeRunner only, no AWS."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from demo_console.actions import ActionError
from demo_console.app import Console, create_app
from demo_console.cloud import CloudAdapter
from demo_console.fakes import FakeGateway, FakeHttp, FakeRunner, make_settings

pytestmark = pytest.mark.unit


def console(
    tmp_path: Path,
    gateway: FakeGateway,
    runner: FakeRunner,
    http: FakeHttp,
    profile: str = "local",
    allow_aws: bool = False,
    cloud: CloudAdapter | None = None,
) -> Console:
    settings = make_settings(tmp_path, profile=profile, TEMPORAL_BUILD_ID="4ee7f87")
    settings.allow_aws = allow_aws
    app = create_app(
        settings, clients=gateway, runner=runner, http=http, cloud=cloud, static_dir=tmp_path
    )
    return app.state.console


async def ready(c: Console) -> Console:
    await c.hub.stack_cache.get()
    return c


async def run(
    c: Console, action: str, params: dict[str, Any] | None = None, confirm: bool = False
) -> tuple[int, dict[str, Any]]:
    status, job = await c.actions.run(action, params or {}, confirm)
    await c.jobs.wait(job["id"], within=5)
    return status, c.jobs.view(c.jobs.jobs[job["id"]])


async def test_register_version_argv_per_mode(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp
) -> None:
    c = await ready(console(tmp_path, gateway, runner, http))
    status, job = await c.actions.run("register-version", {"mode": "--local-wci"}, False)
    assert status == 202 and job["status"] == "running"
    argv, env, _ = runner.spawned[-1]
    assert argv == ["make", "-s", "register-version", "MODE=--local-wci"]
    assert (
        env["TEMPORAL_BUILD_ID"] == "4ee7f87"
        and job["command"] == "make register-version MODE=--local-wci"
    )
    await c.actions.run("register-version", {"mode": "--poller"}, False)
    assert runner.spawned[-1][0] == ["make", "-s", "register-version", "MODE=--poller"]
    with pytest.raises(ActionError) as err:
        await c.actions.run("register-version", {"mode": "--agentcore-auto"}, True)
    assert err.value.code == "aws_disabled"
    with pytest.raises(ActionError) as err:
        await c.actions.run("register-version", {"mode": "--bogus"}, False)
    assert err.value.code == "invalid_param"


async def test_cost_gate(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp
) -> None:
    c = await ready(console(tmp_path, gateway, runner, http))
    with pytest.raises(ActionError) as err:
        await c.actions.run("start-recipe", {"recipe": "local-plain", "variant": "live"}, False)
    assert err.value.code == "confirm_required" and "tokens" in err.value.message
    with pytest.raises(ActionError) as err:
        await c.actions.run("load", {"n": 21}, False)
    assert err.value.code == "invalid_param"
    with pytest.raises(ActionError) as err:
        await c.actions.run("kind-up", {}, True)
    assert err.value.code == "precondition_failed"
    with pytest.raises(ActionError) as err:
        await c.actions.run("nope", {}, False)
    assert err.value.code == "not_found"


async def test_load_and_wci_history(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp
) -> None:
    c = await ready(console(tmp_path, gateway, runner, http))
    await c.actions.run("load", {"n": "3", "kind": "ping"}, False)
    argv, env, _ = runner.spawned[-1]
    assert argv == ["make", "-s", "load", "N=3"] and env["LOAD_KIND"] == "ping"
    await c.actions.run("wci-history", {}, False)
    assert runner.spawned[-1][0] == ["make", "-s", "wci-history", "BUILD=4ee7f87"]


async def test_eks_failure_and_drain_need_aws(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp
) -> None:
    c = await ready(console(tmp_path, gateway, runner, http, profile="eks-pf"))
    with pytest.raises(ActionError) as err:
        await c.actions.run("failure", {}, True)
    assert err.value.code == "aws_disabled"
    assert runner.spawned == []
    allowed = await ready(
        console(tmp_path / "aws", gateway, runner, http, profile="eks-pf", allow_aws=True)
    )
    with pytest.raises(ActionError) as err:
        await allowed.actions.run("failure", {"workflow_id": "support-1042-failure-x"}, False)
    assert err.value.code == "confirm_required"
    status, job = await allowed.actions.run(
        "failure", {"workflow_id": "support-1042-failure-x"}, True
    )
    assert status == 202 and job["cost"] == "aws"
    argv, env, _ = runner.spawned[-1]
    assert argv == ["make", "-s", "failure", "WF=support-1042-failure-x"]
    assert env["TEMPORAL_PROFILE"] == "eks-pf" and "TEMPORAL_TLS" not in env
    assert job["command"] == "make failure WF=support-1042-failure-x TEMPORAL_PROFILE=eks-pf"
    # pf on eks-pf is kubectl to EKS (aws token), so it is gated too.
    assert allowed.actions.describe("pf")["cost"] == "aws"


async def test_drain_cloudwatch_with_fake_logs(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp
) -> None:
    class Session:
        session_id = "0123456789abcdef"
        drained = True

        def first(self, marker: str) -> int | None:
            return {
                "app_start": 1793636000000,
                "invoked": 1793636000100,
                "released": 1793636050000,
            }.get(marker)

        def count(self, marker: str) -> int:
            return 1

    calls: list[tuple[Any, ...]] = []

    def wait_drained(wf: str, since: int, within: float, build: str | None) -> list[Any]:
        calls.append((wf, since, within, build))
        return [Session()]

    settings_cloud = CloudAdapter(
        make_settings(tmp_path / "c", profile="eks-pf"),
        wait_drained=wait_drained,
        sessions_for=lambda *a: [],
    )
    settings_cloud.settings.allow_aws = True
    c = await ready(
        console(
            tmp_path, gateway, runner, http, profile="eks-pf", allow_aws=True, cloud=settings_cloud
        )
    )
    status, job = await run(
        c, "drain-cloudwatch", {"workflow_id": "support-2077-v1-ab"}, confirm=True
    )
    assert status == 202 and job["status"] == "succeeded"
    assert calls and calls[0][0] == "support-2077-v1-ab" and calls[0][3] == "4ee7f87"
    assert job["result"]["drained_at"] == "2026-11-02T16:14:10.000Z"
    assert c.runs.record("support-2077-v1-ab")["cloudwatch"]["sessions"][0]["session"] == "01234567"


async def test_invoke_local_is_synchronous(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp
) -> None:
    http.post_answers["http://127.0.0.1:8080/invocations"] = (
        200,
        {"message": "worker starting", "lifetime": 2, "build_id": "4ee7f87"},
    )
    http.get_answers["http://127.0.0.1:8080/ping"] = (200, {"status": "HealthyBusy"})
    c = await ready(console(tmp_path, gateway, runner, http))
    status, job = await c.actions.run("invoke-local", {}, False)
    assert status == 200 and job["status"] == "succeeded"
    assert job["result"] == {"message": "worker starting", "lifetime": 2, "ping": "HealthyBusy"}
    _, body, headers = http.posts[0]
    assert body == {"deploymentName": "support-supervisor", "buildId": "4ee7f87"}
    assert headers["X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"].startswith("console-")
    http.post_answers["http://127.0.0.1:8080/invocations"] = (200, {"error": "wrong build"})
    status, job = await c.actions.run("invoke-local", {}, False)
    assert job["status"] == "failed" and job["result"] == {"error": "the app refused: wrong build"}


async def test_recipe_local_plain_order(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp
) -> None:
    class DevRunner(FakeRunner):
        async def run(self, argv, *, env, cwd, within=60.0):
            if argv[:3] == ["make", "-s", "dev-server-up"]:
                (tmp_path / ".temporal" / "dev-server.pid").write_text("555")
                self.alive_pids.add(555)
            return await super().run(argv, env=env, cwd=cwd, within=within)

    dev = DevRunner()
    c = await ready(console(tmp_path, gateway, dev, http))
    _, job = await run(c, "start-recipe", {"recipe": "local-plain"})
    assert job["status"] == "succeeded", job["log_tail"]
    ran = [" ".join(a) for a, _ in dev.ran]
    spawned = [a[2] for a, _, _ in dev.spawned]
    assert ran == ["make -s dev-server-up WCI=0", "make -s nexus-endpoint"]
    assert spawned == ["payments-mock", "worker-finance-scripted", "worker-support-scripted"]
    log = job["log_tail"]
    assert (
        log.index("+ start payments-mock")
        < log.index("$ make nexus-endpoint")
        < log.index("+ start worker-support (scripted)")
    )
    # The same recipe with the live variant restarts the model workers on their live targets
    # (README "Local, live Opus 5.5"); payments and the dev server stay as they are.
    _, job = await run(c, "start-recipe", {"recipe": "local-plain", "variant": "live"}, True)
    assert job["status"] == "succeeded", job["log_tail"]
    spawned = [a[2] for a, _, _ in dev.spawned]
    assert spawned[3:] == ["worker-finance", "worker-support"]
    assert "~ restart worker-finance: scripted -> live" in job["log_tail"]
    assert "= payments-mock already running" in job["log_tail"]
    assert c.procs.managed_variant("worker-support") == "live"
    # Teardown stops what the console started, dev server last.
    _, job = await run(c, "start-recipe", {"recipe": "teardown"})
    assert job["result"]["stopped"][-1] == "dev-server"
    assert set(job["result"]["stopped"]) == {
        "payments-mock",
        "worker-finance",
        "worker-support",
        "dev-server",
    }


async def test_actions_listing(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp
) -> None:
    c = await ready(console(tmp_path, gateway, runner, http))
    actions = {a["id"]: a for a in c.actions.describe_all()}
    assert actions["register-version"]["params"][0]["default"] == "--poller"
    assert actions["register-version"]["make"] == "make register-version MODE=--poller"
    assert actions["failure"]["available"] is False and actions["failure"]["cost"] == "aws"
    assert actions["kind-down"]["long"] is True
    kind = await ready(console(tmp_path / "k", gateway, runner, http, profile="kind"))
    k = {a["id"]: a for a in kind.actions.describe_all()}
    assert k["kind-temporal"]["make"] == "make kind-temporal KIND_TLS=1 TEMPORAL_PROFILE=kind"
    assert k["start-recipe"]["params"][0]["default"] == "kind-laptop"
