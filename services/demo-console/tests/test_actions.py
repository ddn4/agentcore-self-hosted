"""Actions, the cost gate and recipes (DESIGN.md 2.3, 4.5, 5.5): FakeRunner only, no AWS."""

from __future__ import annotations

import asyncio
import json
import signal
from pathlib import Path
from typing import Any

import pytest

from demo_console.actions import SPECS, ActionError
from demo_console.app import Console, create_app
from demo_console.fakes import FakeGateway, FakeHttp, FakeRunner, make_settings
from demo_console.jobs import Job
from demo_console.payloads import iso, now
from demo_console.settings import PROFILES

pytestmark = pytest.mark.unit


def console(
    tmp_path: Path,
    gateway: FakeGateway,
    runner: FakeRunner,
    http: FakeHttp,
    profile: str = "local",
) -> Console:
    settings = make_settings(tmp_path, profile=profile, TEMPORAL_BUILD_ID="4ee7f87")
    app = create_app(settings, clients=gateway, runner=runner, http=http, static_dir=tmp_path)
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
    assert err.value.code == "invalid_param"
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
    argv = runner.spawned[-1][0]
    # LOAD_KIND is a make variable on the command line: make exports it to load.sh.
    assert argv == ["make", "-s", "load", "N=3", "LOAD_KIND=ping"]
    await c.actions.run("wci-history", {}, False)
    assert runner.spawned[-1][0] == ["make", "-s", "wci-history", "BUILD=4ee7f87"]


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
    assert "failure" not in actions and "drain-cloudwatch" not in actions
    assert actions["kind-down"]["long"] is True
    kind = await ready(console(tmp_path / "k", gateway, runner, http, profile="kind"))
    k = {a["id"]: a for a in kind.actions.describe_all()}
    assert k["kind-temporal"]["make"] == "make kind-temporal KIND_TLS=1 TEMPORAL_PROFILE=kind"
    assert k["start-recipe"]["params"][0]["default"] == "kind-up"
    assert k["kind-workers"]["make"] == (
        "make kind-workers FINANCE_MODEL=scripted KIND_TLS=1 TEMPORAL_PROFILE=kind"
    )


# --- one source for each command ---------------------------------------------------------------


@pytest.mark.parametrize("profile", PROFILES)
async def test_the_shown_command_is_the_argv_that_runs(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp, profile: str
) -> None:
    c = await ready(console(tmp_path, gateway, runner, http, profile=profile))
    for spec in SPECS.values():
        if spec.in_process or profile not in spec.profiles:
            continue
        params = c.actions._validate(spec, {})
        argv = c.actions.argv(spec, params, "4ee7f87", "ab12")
        assert argv[:3] == ["make", "-s", spec.make[0]], spec.id
        shown = c.actions.make_text(spec, params, "4ee7f87", "ab12")
        assert shown == " ".join(["make", *argv[2:]]), spec.id
    if profile == "kind-plaintext":
        listing = {a["id"]: a for a in c.actions.describe_all()}
        assert listing["kind-workers"]["make"] == (
            "make kind-workers FINANCE_MODEL=scripted KIND_TLS=0 TEMPORAL_PROFILE=kind-plaintext"
        )


async def test_a_job_runs_and_logs_the_command_it_shows(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp
) -> None:
    c = await ready(console(tmp_path, gateway, runner, http, profile="kind-plaintext"))
    _, job = await c.actions.run("kind-workers", {}, confirm=True)
    argv = runner.spawned[-1][0]
    assert job["command"] == " ".join(["make", *argv[2:]])
    assert "KIND_TLS=0" in argv


# --- job lifecycle -------------------------------------------------------------------------------


async def test_a_command_that_cannot_start_fails_its_job(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp
) -> None:
    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError(2, "No such file or directory", "make")

    runner.spawn = broken  # type: ignore[method-assign]
    c = await ready(console(tmp_path, gateway, runner, http))
    status, job = await c.actions.run("nexus-endpoint", {}, False)
    assert status == 202 and job["status"] == "failed"
    assert job["result"]["error"].startswith("make could not start")
    assert not c.jobs.running()


async def test_a_command_past_its_time_is_stopped_and_fails(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp
) -> None:
    c = await ready(console(tmp_path, gateway, runner, http))
    job = await c.jobs.run_command("card", ["make", "-s", "card"], env={}, within=0.3)
    pgid = job.pgid
    ended = await c.jobs.wait(job.id, within=5)
    assert ended.status == "failed"
    assert "ran longer than 0.3s and was stopped" in ended.result["error"]
    assert pgid is not None and (pgid, signal.SIGTERM) in runner.signals


async def test_an_adopted_job_ends_without_claiming_success(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp
) -> None:
    c = console(tmp_path, gateway, runner, http)
    record = Job(
        id="j-pf",
        action="pf",
        command="make pf",
        cost="free",
        started_at=iso(now()) or "",
        pgid=4343,
    )
    jobs_dir = c.settings.state_dir / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (jobs_dir / "j-pf.json").write_text(json.dumps(record.record()))
    runner.alive_groups.add(4343)
    again = console(tmp_path, gateway, runner, http)
    again.jobs.adopt()
    runner.alive_groups.discard(4343)
    ended = await again.jobs.wait("j-pf", within=5)
    assert ended.status == "ended" and ended.exit_code is None
    assert "exit code is unknown" in ended.result["note"]


async def test_a_sync_action_still_running_answers_202(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp
) -> None:
    c = await ready(console(tmp_path, gateway, runner, http))

    async def slow(log: Any) -> dict[str, Any]:
        await asyncio.sleep(10)
        return {}

    c.actions._ping_body = slow  # type: ignore[method-assign]

    real_wait = c.jobs.wait

    async def quick(job_id: str, within: float | None = None) -> Job:
        return await real_wait(job_id, within=0.05)

    c.jobs.wait = quick  # type: ignore[method-assign]
    status, job = await c.actions.run("agentcore-ping", {}, False)
    assert status == 202 and job["status"] == "running"
    await c.jobs.shutdown()


async def test_one_recipe_at_a_time(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp
) -> None:
    c = await ready(console(tmp_path, gateway, runner, http))
    gate = asyncio.Event()

    async def held(log: Any, recipe: str, variant: str) -> dict[str, Any]:
        await gate.wait()
        return {}

    async def build_id() -> str:
        await asyncio.sleep(0)  # both clicks pass the first check before either job exists
        return "4ee7f87"

    c.actions.recipe = held  # type: ignore[method-assign]
    c.actions.stack.build_id = build_id  # type: ignore[method-assign]
    first, second = await asyncio.gather(
        c.actions.run("start-recipe", {"recipe": "local-invoke"}, True),
        c.actions.run("start-recipe", {"recipe": "local-invoke"}, True),
        return_exceptions=True,
    )
    results = [first, second]
    errors = [r for r in results if isinstance(r, ActionError)]
    assert len(errors) == 1, results
    assert errors[0].code == "precondition_failed"  # 409
    assert errors[0].message == "Local, serverless (invoke-local) is still running."
    assert errors[0].hint and "One stack job runs at a time" in errors[0].hint
    gate.set()
    await c.jobs.shutdown()


def test_the_catalog_names_real_actions_and_labels_every_step() -> None:
    from demo_console.scenarios import load_catalog

    # load.sh bounds its own Workflows at 15 min; the job limit must not cut it short.
    assert SPECS["load"].within > 15 * 60

    for s in load_catalog().values():
        if s.start_action:
            spec = SPECS[s.start_action]
            # The Start button's options are the action's params, nothing else.
            assert {o["id"] for o in s.options} <= {p["id"] for p in spec.params}, s.id
        if s.extra_action:
            assert s.extra_action["action"] in SPECS and s.extra_action["label"], s.id
        assert s.view in (None, "cleanup"), s.id
        for step in s.steps:
            assert step.short, (s.id, step.id)
            if step.action and step.action not in ("start-run", "approve", "reject"):
                assert step.action in SPECS or step.action.startswith("process."), step.action


def test_a_scenario_whose_start_runs_an_action_offers_only_its_params() -> None:
    from demo_console.scenarios import StackFacts, load_catalog, scenario_json

    catalog = load_catalog()
    body = scenario_json(catalog["load"], StackFacts(profile="local"), catalog=catalog)
    assert [o["id"] for o in body["options"]] == ["n", "kind"]
    assert body["start_action"] == "load" and body["start_label"] == "Start {n} conversations"


async def test_setup_restarts_an_app_on_a_stale_build(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp
) -> None:
    # Recipes register the current Build ID; an app started on an older one would refuse every
    # invocation, so "already running" is not good enough for it.
    c = await ready(console(tmp_path, gateway, runner, http))
    await c.procs.start("agentcore-local", variant="live", confirm=True)
    c.procs.records["agentcore-local"].build_id = "79663e2"

    class Log:
        def __init__(self) -> None:
            self.lines: list[str] = []

        def line(self, text: str) -> None:
            self.lines.append(text)

    log = Log()
    await c.actions._ensure(log, "agentcore-local", None, build="4ee7f87")  # type: ignore[arg-type]
    assert log.lines[0] == "~ restart agentcore-local: Build ID 79663e2 -> 4ee7f87"
    assert runner.spawned[-1][0] == ["make", "-s", "agentcore-local"]  # kept the live model
    assert runner.spawned[-1][1]["TEMPORAL_BUILD_ID"] == "4ee7f87"
    assert c.procs.stale_build("agentcore-local", "4ee7f87") is None
    await c.actions._ensure(log, "agentcore-local", None, build="4ee7f87")  # type: ignore[arg-type]
    assert log.lines[-1] == "= agentcore-local already running"
