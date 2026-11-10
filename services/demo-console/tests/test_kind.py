"""The kind stack from the console (DESIGN.md 2.3, 5.1, 5.5): layer facts, the "Bring up kind"
recipe's order and skips, failure causes, and the recipe/profile contract. Fakes only."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from demo_console.actions import RECIPES, SPECS, ActionError
from demo_console.app import Console, create_app
from demo_console.fakes import FakeGateway, FakeHttp, FakeRunner, make_settings
from demo_console.kind import KIND_CLUSTER
from demo_console.payloads import iso, now
from demo_console.settings import PROFILES

pytestmark = pytest.mark.unit

DOCKER = "docker --context colima-agentcore info"
CLUSTERS = "kind get clusters"
POSTGRES = "kubectl --context kind-agentcore-demo get deploy/postgres"
HELM_STATUS = "helm --kube-context kind-agentcore-demo status temporal"
HELM_VALUES = "helm --kube-context kind-agentcore-demo get values temporal"
FINANCE_IMAGE = "kubectl --context kind-agentcore-demo get deploy/agent-services-worker"
CLUSTER_CA = "kubectl --context kind-agentcore-demo get secret temporal-operator-tls"
LONG = ("make -s kind-up", "make -s kind-temporal", "make -s kind-workers")


def console(
    tmp_path: Path,
    gateway: FakeGateway,
    runner: FakeRunner,
    http: FakeHttp,
    profile: str = "kind",
) -> Console:
    settings = make_settings(tmp_path, profile=profile, TEMPORAL_BUILD_ID="4ee7f87")
    for name in ("operator", "agentcore-worker", "laptop-support"):
        (tmp_path / ".certs" / name).mkdir(parents=True, exist_ok=True)
        (tmp_path / ".certs" / name / "tls.crt").write_text("cert")
    (tmp_path / ".certs" / "ca.crt").write_text("CA-1\n")
    app = create_app(settings, clients=gateway, runner=runner, http=http, static_dir=tmp_path)
    return app.state.console


def laptop_answers(http: FakeHttp) -> None:
    http.get_answers["http://127.0.0.1:8080/ping"] = (200, {"status": "Healthy"})
    http.post_answers["http://127.0.0.1:8080/invocations"] = (
        200,
        {"message": "worker starting", "lifetime": 1},
    )


def warm_cluster(runner: FakeRunner, tmp_path: Path, http: FakeHttp, gateway: FakeGateway) -> None:
    runner.result_for(CLUSTERS, 0, f"{KIND_CLUSTER}\n")
    runner.result_for(POSTGRES, 0, "1")
    runner.result_for(HELM_STATUS, 0, '{"info": {"status": "deployed"}}')
    runner.result_for(HELM_VALUES, 0, '{"server": {"tls": {"frontend": {"enabled": true}}}}')
    runner.result_for(FINANCE_IMAGE, 0, "agentcore-demo/finance-agent:kind-scripted")
    (tmp_path / ".temporal").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".temporal" / "kind-pf-frontend.pid").write_text("777")
    runner.alive_pids.add(777)
    http.ports.add(7233)
    gateway.pollers_by_queue["agent-services/agent-services-tq"] = [
        {"identity": "1@finance-pod", "last_access": iso(now())}
    ]


async def start_recipe(c: Console, recipe: str, variant: str = "scripted") -> dict[str, Any]:
    await c.hub.stack_cache.get()
    _, job = await c.actions.run(
        "start-recipe", {"recipe": recipe, "variant": variant}, confirm=True
    )
    await c.jobs.wait(job["id"], within=10)
    return c.jobs.view(c.jobs.jobs[job["id"]], lines=200)


def makes(job: dict[str, Any]) -> list[str]:
    return [line[2:] for line in job["log_tail"] if line.startswith("$ make ")]


def statuses(job: dict[str, Any]) -> dict[str, str]:
    return {s["id"]: s["status"] for s in job["steps"]}


# --- the recipe/profile contract ---------------------------------------------------------------


@pytest.mark.parametrize("profile", PROFILES)
async def test_every_offered_recipe_is_accepted(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp, profile: str
) -> None:
    c = console(tmp_path, gateway, runner, http, profile=profile)
    await c.hub.stack_cache.get()
    listing = c.actions.describe("start-recipe")
    offered = [r for r in listing["recipes"] if profile in r["profiles"]]
    assert offered, profile
    spec = SPECS["start-recipe"]
    for r in offered:
        c.actions._validate(spec, {"recipe": r["id"]})
        for variant, steps in r["steps"].items():
            ids = [s["id"] for s in steps]
            assert len(ids) == len(set(ids)), (r["id"], variant)
            assert set(ids) <= {s.id for s in RECIPES[r["id"]].steps}
    for r in listing["recipes"]:
        if profile not in r["profiles"]:
            with pytest.raises(ActionError) as err:
                c.actions._validate(spec, {"recipe": r["id"]})
            assert err.value.code == "precondition_failed"
    # Teardown is local only; kind's default is the full bring-up, marked long.
    kinds = {r["id"] for r in listing["recipes"] if "kind" in r["profiles"]}
    assert "teardown" not in kinds and {"kind-up", "kind-laptop"} <= kinds
    bring_up = next(r for r in listing["recipes"] if r["id"] == "kind-up")
    assert bring_up["long"] is True


async def test_bring_up_plan_follows_the_profile(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp
) -> None:
    mtls = console(tmp_path / "a", gateway, runner, http, profile="kind")
    plain = console(tmp_path / "b", gateway, runner, http, profile="kind-plaintext")
    r = RECIPES["kind-up"]
    a = [s["label"] for s in mtls.actions.recipe_plan(r, "scripted")]
    b = [s["label"] for s in plain.actions.recipe_plan(r, "live")]
    assert "certs-pull" in a and "certs-pull" not in b
    assert "kind-temporal KIND_TLS=1, unless installed" in a
    assert "kind-temporal KIND_TLS=0, unless installed" in b
    assert "kind-aws-creds" in b and "kind-aws-creds" not in a
    assert "agentcore-local-scripted" in a and "agentcore-local" in b


# --- Bring up kind -------------------------------------------------------------------------------


async def test_bring_up_kind_from_a_cold_docker_vm_runs_every_layer(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp
) -> None:
    laptop_answers(http)
    for prefix in LONG:
        runner.auto_exit[prefix] = (0, "ok\n")
    c = console(tmp_path, gateway, runner, http)
    job = await start_recipe(c, "kind-up")
    assert job["status"] == "succeeded", job["log_tail"]
    assert makes(job) == [
        "make kind-up",
        "make kind-temporal KIND_TLS=1",
        "make certs-pull",
        "make pf",
        "make kind-workers FINANCE_MODEL=scripted KIND_TLS=1",
        "make nexus-endpoint",
        "make register-version MODE=--poller",
    ]
    assert set(statuses(job).values()) == {"done"}
    assert [s["id"] for s in job["steps"]][:2] == ["docker", "kind-up"]
    # The long cluster steps stream (spawned into the job log), with the kind env.
    streamed = [argv for argv, env, _ in runner.spawned if argv[2] in ("kind-up", "kind-temporal")]
    assert len(streamed) == 2
    env = next(env for argv, env, _ in runner.spawned if argv[2] == "kind-up")
    assert env["DOCKER_CONTEXT"] == "colima-agentcore"
    assert env["KUBECONFIG"].endswith(".temporal/kubeconfig-agentcore-demo")
    assert c.procs.managed_variant("agentcore-local") == "scripted"


async def test_bring_up_kind_skips_layers_that_are_up(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp
) -> None:
    laptop_answers(http)
    warm_cluster(runner, tmp_path, http, gateway)
    c = console(tmp_path, gateway, runner, http)
    job = await start_recipe(c, "kind-up")
    assert job["status"] == "succeeded", job["log_tail"]
    st = statuses(job)
    assert st["kind-up"] == st["kind-temporal"] == st["kind-workers"] == "skipped"
    assert makes(job) == [
        "make certs-pull",
        "make nexus-endpoint",
        "make register-version MODE=--poller",
    ]
    assert "= port-forward already running" in job["log_tail"]


async def test_plaintext_reinstalls_a_tls_release(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp
) -> None:
    laptop_answers(http)
    warm_cluster(runner, tmp_path, http, gateway)
    runner.auto_exit["make -s kind-temporal"] = (0, "")
    c = console(tmp_path, gateway, runner, http, profile="kind-plaintext")
    job = await start_recipe(c, "kind-up")
    assert job["status"] == "succeeded", job["log_tail"]
    assert "make kind-temporal KIND_TLS=0" in makes(job)
    assert "make certs-pull" not in makes(job)


async def test_bring_up_kind_stops_at_a_stopped_docker_vm(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp
) -> None:
    runner.result_for(DOCKER, 1, "Cannot connect to the Docker daemon")
    c = console(tmp_path, gateway, runner, http)
    job = await start_recipe(c, "kind-up")
    assert job["status"] == "failed"
    assert job["result"]["step"] == "docker"
    assert job["result"]["error"] == "Docker VM agentcore is stopped"
    assert "Start Docker VM" in job["result"]["hint"]
    assert makes(job) == []
    assert statuses(job)["kind-up"] == "todo"


async def test_a_failed_step_carries_its_cause(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp
) -> None:
    runner.result_for(CLUSTERS, 0, f"{KIND_CLUSTER}\n")
    runner.result_for(POSTGRES, 0, "1")
    runner.auto_exit["make -s kind-temporal"] = (
        2,
        "[kind] helm upgrade --install temporal\n"
        "Error: context deadline exceeded\n"
        "make: *** [kind-temporal] Error 1\n",
    )
    c = console(tmp_path, gateway, runner, http)
    job = await start_recipe(c, "kind-up")
    res = job["result"]
    assert job["status"] == "failed"
    assert res["step"] == "kind-temporal"
    assert res["step_label"] == "kind-temporal KIND_TLS=1, unless installed"
    assert res["error"] == "Error: context deadline exceeded"
    assert "make: ***" not in " ".join(res["tail"])
    assert statuses(job)["kind-temporal"] == "failed"


async def test_laptop_side_only_refuses_without_temporal(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp
) -> None:
    c = console(tmp_path, gateway, runner, http)
    await c.hub.stack_cache.get()
    with pytest.raises(ActionError) as err:
        await c.actions.run("start-recipe", {"recipe": "kind-laptop"}, False)
    assert err.value.code == "precondition_failed"
    assert err.value.message == "Temporal on kind is not reachable: run Bring up kind first."
    assert runner.ran == [r for r in runner.ran if r[0][0] != "make"]


async def test_cluster_jobs_run_one_at_a_time(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp
) -> None:
    c = console(tmp_path, gateway, runner, http)
    await c.hub.stack_cache.get()
    await c.actions.run("pf", {}, True)  # a spawned child that keeps running
    with pytest.raises(ActionError) as err:
        await c.actions.run("start-recipe", {"recipe": "kind-up"}, True)
    assert err.value.message == "Start port-forward is still running."
    with pytest.raises(ActionError):
        await c.actions.run("kind-up", {}, True)
    await c.actions.run("nexus-endpoint", {}, False)  # not a cluster job


# --- the stack snapshot on kind -------------------------------------------------------------------


def component(snap: dict[str, Any], cid: str) -> dict[str, Any]:
    return next(c for c in snap["components"] if c["id"] == cid)


async def test_snapshot_names_the_first_missing_layer(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp
) -> None:
    gateway.up = False
    runner.result_for(DOCKER, 1, "Cannot connect to the Docker daemon")
    c = console(tmp_path, gateway, runner, http)
    snap = await c.stack.snapshot()
    cluster = component(snap, "cluster")
    assert cluster["status"] == "down" and cluster["detail"] == "Docker VM agentcore is stopped"
    assert cluster["fix"]["action"] == "colima-up"
    assert snap["kind"]["gap"]["layer"] == "docker" and snap["kind"]["cluster"] is None
    actions = {a["id"]: a for a in c.actions.describe_all()}
    assert actions["kind-temporal"]["why_not"].startswith("Docker VM agentcore is stopped")
    assert actions["colima-up"]["available"] is True
    # Docker up, no cluster: the fix is the bring-up recipe.
    runner.result_for(DOCKER, 0, "28.0")
    c.stack.kind.invalidate()
    snap = await c.stack.snapshot()
    assert snap["kind"]["gap"]["layer"] == "cluster"
    assert component(snap, "cluster")["fix"] == {
        "kind": "action",
        "action": "start-recipe",
        "params": {"recipe": "kind-up"},
        "label": "Bring up kind",
    }
    assert c.actions.describe("colima-up")["why_not"] == "Docker VM agentcore is running."
    # Cluster up, port-forward off.
    runner.result_for(CLUSTERS, 0, f"{KIND_CLUSTER}\n")
    c.stack.kind.invalidate()
    snap = await c.stack.snapshot()
    assert snap["kind"]["gap"]["cause"] == "Port-forward is off"
    assert component(snap, "cluster")["fix"]["action"] == "pf"
    # The local profile has no cluster facts.
    local = console(tmp_path / "l", gateway, runner, http, profile="local")
    snap = await local.stack.snapshot()
    assert snap["kind"] is None and all(c["id"] != "cluster" for c in snap["components"])


async def test_certs_from_an_older_cluster_are_stale(
    tmp_path: Path, gateway: FakeGateway, runner: FakeRunner, http: FakeHttp
) -> None:
    warm_cluster(runner, tmp_path, http, gateway)
    runner.result_for(CLUSTER_CA, 0, "Q0EtMgo=")  # base64 "CA-2\n"; the laptop has CA-1
    c = console(tmp_path, gateway, runner, http)
    snap = await c.stack.snapshot()
    assert snap["kind"]["certs"] == "stale"
    cluster = component(snap, "cluster")
    assert cluster["status"] == "degraded" and cluster["fix"]["action"] == "certs-pull"
    assert "older cluster" in (c.procs.describe("agentcore-local")["why"] or "")
    runner.result_for(CLUSTER_CA, 0, "Q0EtMQo=")  # "CA-1\n"
    c.stack.kind.invalidate()
    snap = await c.stack.snapshot()
    assert snap["kind"]["certs"] == "ok" and component(snap, "cluster")["status"] == "up"
