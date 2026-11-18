"""Actions: one-shot jobs, recipes and the cost gate (DESIGN.md 2.3, 4.5, 5.5).

Every action has a cost (``free`` | ``tokens``). ``tokens`` needs ``confirm: true``; long kind
lifecycle jobs (and the long recipe "Bring up kind") need ``confirm`` too. Commands are
``make -s <target>`` so the Makefile stays the source of truth; invoke-local and the ping are
done in-process (plain HTTP).

Recipes are data (``RECIPES``): each one's checklist is served by ``GET /api/actions`` and is
the same list the recipe body walks (``JobLog.step`` refuses an id that is not in the plan).
"""

from __future__ import annotations

import asyncio
import random
import re
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from demo_console.jobs import Job, JobLog, JobStore, StepError, cause_line, meaningful
from demo_console.kind import (
    COLIMA_PROFILE,
    KIND_CLUSTER,
    cluster_exists,
    docker_up,
    finance_image,
    helm_release,
    kind_env,
    postgres_ready,
)
from demo_console.procs import ProcessError, ProcessManager
from demo_console.scenarios import shape_recipe, suffix
from demo_console.settings import (
    DEPLOYMENT_NAME,
    FINANCE_TQ,
    KIND_PROFILES,
    PROFILES,
    SERVICES_NS,
    Settings,
)
from demo_console.stack import HttpProbe, StackProbe, fresh, ping_status

KIND = KIND_PROFILES
ALL = PROFILES
#: Steps that may run longer than a plain make call: they stream into the job log.
SHORT_S = 180.0


@dataclass(frozen=True)
class RecipeStep:
    id: str
    #: What the checklist shows. ``{v}`` is ``-scripted`` for the scripted variant, ``{tls}``
    #: the profile's KIND_TLS, ``{model}`` the Finance model on kind.
    label: str
    profiles: tuple[str, ...] | None = None
    variants: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Recipe:
    id: str
    title: str
    profiles: tuple[str, ...]
    steps: tuple[RecipeStep, ...]
    long: bool = False
    danger: bool = False
    variants: bool = True
    needs_build: bool = False


def _s(id: str, label: str | None = None, **kw: Any) -> RecipeStep:
    return RecipeStep(id, label or id, **kw)


_LAPTOP_SIDE = (
    _s("nexus-endpoint"),
    _s("agentcore-local", "agentcore-local{v}"),
    _s("ping", "wait for /ping"),
    _s("invoke-local"),
    _s("register-version", "register-version MODE=--poller"),
)
_LOCAL_BASE = (
    _s("payments-mock"),
    _s("worker-finance", "worker-finance{v}"),
    _s("nexus-endpoint"),
)

RECIPES: dict[str, Recipe] = {
    r.id: r
    for r in (
        Recipe(
            "local-plain",
            "Local, plain Support",
            ("local",),
            (
                _s("dev-server", "dev-server-up WCI=0"),
                _s("stop", "stop agentcore-local, agentcore-emulator"),
                *_LOCAL_BASE,
                _s("worker-support", "worker-support{v}"),
            ),
        ),
        Recipe(
            "local-invoke",
            "Local, serverless (invoke-local)",
            ("local",),
            (
                _s("dev-server", "dev-server-up WCI=0"),
                _s("stop", "stop worker-support"),
                *_LOCAL_BASE,
                *_LAPTOP_SIDE[1:],
            ),
            needs_build=True,
        ),
        Recipe(
            "local-wci",
            "Local, serverless (WCI emulator)",
            ("local",),
            (
                _s("drain", "wait for /ping Healthy"),
                _s("dev-server", "dev-server-up WCI=1"),
                _s("stop", "stop worker-support"),
                *_LOCAL_BASE,
                _s("agentcore-emulator"),
                _s("agentcore-local", "agentcore-local{v}"),
                _s("ping", "wait for /ping"),
                _s("register-version", "register-version MODE=--local-wci"),
            ),
            needs_build=True,
        ),
        Recipe(
            "kind-up",
            "Bring up kind",
            KIND,
            (
                _s("docker", f"check Docker VM {COLIMA_PROFILE}"),
                _s("kind-up", "kind-up, unless the cluster is up"),
                _s("kind-temporal", "kind-temporal KIND_TLS={tls}, unless installed"),
                _s("certs-pull", profiles=("kind",)),
                _s("pf", "pf, wait for Temporal on :7233"),
                _s("kind-aws-creds", variants=("live",)),
                _s("kind-workers", "kind-workers FINANCE_MODEL={model}, unless polling"),
                *_LAPTOP_SIDE,
            ),
            long=True,
            needs_build=True,
        ),
        Recipe(
            "kind-laptop",
            "Laptop side only (cluster already up)",
            KIND,
            (_s("temporal", "check Temporal on :7233"), *_LAPTOP_SIDE),
            needs_build=True,
        ),
        Recipe(
            "teardown",
            "Tear down local",
            ("local",),
            (_s("stop", "stop every managed process"), _s("dev-server", "dev-server-down")),
            danger=True,
            variants=False,
        ),
    )
}

#: Jobs that touch the cluster or the Docker VM: one at a time (with recipes).
CLUSTER = ("colima-up", "kind-up", "kind-temporal", "kind-workers", "certs-pull", "pf")
CLUSTER_ALL = (*CLUSTER, "pf-stop", "kind-down")
EXCLUSIVE = frozenset((*CLUSTER_ALL, "start-recipe"))


class ActionError(Exception):
    def __init__(self, code: str, message: str, hint: str | None = None, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.extra = extra


@dataclass(frozen=True)
class ActionSpec:
    id: str
    label: str
    profiles: tuple[str, ...]
    params: list[dict[str, Any]] = field(default_factory=list)
    long: bool = False
    needs_build: bool = False
    sync: bool = False
    #: Where the sheet lists it: ``recipe | oneshot | cluster``.
    group: str = "oneshot"
    #: ``make -s <target> <VAR=value>...`` as templates over the params, ``{build}``,
    #: ``{tls}`` (the profile's KIND_TLS) and ``{suffix}`` (fresh for each run). The job runs
    #: exactly this argv, and the console shows it without ``-s``: one source for both.
    make: tuple[str, ...] = ()
    #: Run in-process (plain HTTP or a recipe body); ``make`` is then its terminal twin.
    in_process: bool = False
    #: The longest a ``make`` job may run before its process group is stopped.
    within: float = 600.0


SPECS: dict[str, ActionSpec] = {
    a.id: a
    for a in (
        ActionSpec(
            "start-recipe",
            "Set up the stack",
            ("local", *KIND),
            [
                {
                    "id": "recipe",
                    "type": "choice",
                    "choices": list(RECIPES),
                    "default": "local-plain",
                },
                {
                    "id": "variant",
                    "type": "choice",
                    "choices": ["scripted", "live"],
                    "default": "scripted",
                },
            ],
            group="recipe",
            in_process=True,
        ),
        ActionSpec(
            "nexus-endpoint",
            "Create the Nexus endpoint finance-agent",
            ALL,
            make=("nexus-endpoint",),
        ),
        ActionSpec(
            "register-version",
            "Register Support's version",
            ALL,
            [
                {
                    "id": "mode",
                    "type": "choice",
                    "choices": ["--poller", "--local-wci"],
                    "default": "--poller",
                }
            ],
            needs_build=True,
            make=("register-version", "MODE={mode}"),
        ),
        ActionSpec(
            "invoke-local",
            "Invoke AgentCore app",
            ALL,
            needs_build=True,
            sync=True,
            make=("invoke-local", "TEMPORAL_BUILD_ID={build}"),
            in_process=True,
        ),
        ActionSpec(
            "agentcore-ping",
            "Ping the AgentCore app",
            ALL,
            sync=True,
            make=("agentcore-ping",),
            in_process=True,
        ),
        ActionSpec(
            "smoke", "Run the smoke caller", ("local",), make=("smoke", "WF=smoke-1042-{suffix}")
        ),
        ActionSpec("card", "Standalone get_agent_card", ALL, make=("card",)),
        ActionSpec(
            "load",
            "Start N conversations",
            ALL,
            [
                {"id": "n", "type": "number", "default": 10, "min": 1, "max": 20},
                {"id": "kind", "type": "choice", "choices": ["ping", "case-a"], "default": "ping"},
            ],
            make=("load", "N={n}", "LOAD_KIND={kind}"),
            # Above load.sh's own bound (ping: 15 min execution timeout) plus its reads after.
            within=1200.0,
        ),
        ActionSpec(
            "wci-history",
            "Show the WCI's history",
            ALL,
            needs_build=True,
            make=("wci-history", "BUILD={build}"),
        ),
        *(
            ActionSpec(
                id, label, KIND, long=True, group="cluster", make=(id, *extra), within=1800.0
            )
            for id, label, extra in (
                ("colima-up", "Start Docker VM", ()),
                ("kind-up", "Create the kind cluster", ()),
                ("kind-temporal", "Install Temporal on kind", ("KIND_TLS={tls}",)),
                (
                    "kind-workers",
                    "Deploy Finance pods (scripted)",
                    ("FINANCE_MODEL=scripted", "KIND_TLS={tls}"),
                ),
                ("certs-pull", "Pull client certs", ()),
                ("pf", "Start port-forward", ()),
                ("pf-stop", "Stop port-forward", ()),
                ("kind-down", "Delete the kind cluster", ()),
            )
        ),
    )
}

# Fixed hints for known causes (no scraping of arbitrary make output beyond these).
_DOCKER = re.compile(r"Cannot connect to the Docker daemon|docker daemon .*running", re.I)
_CERTS = re.compile(r"x509|unknown authority|certificate (signed|verify)|bad certificate", re.I)
_REFUSED = re.compile(r"127\.0\.0\.1:7233|nothing is listening|connection refused", re.I)
_AWS = re.compile(r"Unable to locate credentials|ExpiredToken|sso session|aws sso login", re.I)


def hint_for(text: str, profile: str) -> str | None:
    if _DOCKER.search(text):
        return f"Start Docker VM {COLIMA_PROFILE} (Manage stack), then run Bring up kind."
    if _CERTS.search(text) and profile == "kind":
        return "The client certs are from an older cluster: Pull client certs (make certs-pull)."
    if _REFUSED.search(text):
        if profile == "local":
            return "Start the dev server (Manage stack)."
        return "Temporal on kind is not reachable: run Bring up kind (Manage stack)."
    if _AWS.search(text):
        return "Run aws sso login, then try again."
    return None


class ActionService:
    def __init__(
        self,
        settings: Settings,
        procs: ProcessManager,
        jobs: JobStore,
        stack: StackProbe,
        http: HttpProbe,
    ) -> None:
        self.settings = settings
        self.procs = procs
        self.jobs = jobs
        self.stack = stack
        self.http = http

    # --- descriptions ------------------------------------------------------------------------

    def _model_live(self) -> bool:
        snap = self.stack.last or {}
        return (snap.get("model") or {}).get("mode") == "live"

    def cost(self, spec: ActionSpec, params: dict[str, Any]) -> str:
        if spec.id == "start-recipe" and params.get("variant") == "live":
            return "tokens"
        if spec.id == "load" and params.get("kind") == "case-a" and self._model_live():
            return "tokens"
        return "free"

    def _defaults(self, spec: ActionSpec) -> dict[str, Any]:
        out = {p["id"]: p.get("default") for p in spec.params}
        if spec.id == "register-version":
            out["mode"] = self._default_mode()
        if spec.id == "start-recipe":
            # The recipe that produces the stack's current shape (the sheet recommends it).
            shape = self.stack.facts.shape
            out["recipe"] = shape_recipe(self.settings.profile, shape) or "local-plain"
        return out

    def _default_mode(self) -> str:
        snap = self.stack.last or {}
        if (snap.get("wci") or {}).get("mode") == "emulator":
            return "--local-wci"
        return "--poller"

    def _why_not(self, spec: ActionSpec, params: dict[str, Any]) -> str | None:
        if self.settings.profile not in spec.profiles:
            return f"Not on profile {self.settings.profile}."
        if spec.needs_build and not self.stack.facts.build_id:
            return "No Build ID. Commit uv.lock, or start with TEMPORAL_BUILD_ID=<id>."
        facts = self.stack.kind.last()
        if facts is not None and spec.group == "cluster":
            if spec.id == "colima-up":
                return f"Docker VM {COLIMA_PROFILE} is running." if facts.docker else None
            if not facts.docker and spec.id != "pf-stop":
                return f"Docker VM {COLIMA_PROFILE} is stopped: Start Docker VM first."
            needs_cluster = ("kind-temporal", "kind-workers", "certs-pull", "pf")
            if facts.cluster is False and spec.id in needs_cluster:
                return "No kind cluster: run Bring up kind."
        return None

    def argv(
        self, spec: ActionSpec, params: dict[str, Any], build: str | None, tag: str
    ) -> list[str]:
        """``make -s <target> <VAR=value>...`` for ``spec``: what a ``make`` job runs."""
        values = {**params, "build": build or "<build>", "tls": self._kind_tls(), "suffix": tag}
        argv = ["make", "-s", *(part.format(**values) for part in spec.make)]
        if self.settings.profile != "local":
            argv.append(f"TEMPORAL_PROFILE={self.settings.profile}")
        return argv

    def make_text(
        self,
        spec: ActionSpec,
        params: dict[str, Any],
        build: str | None = None,
        tag: str = "<id>",
    ) -> str:
        """The command the console shows: its argv without ``-s`` (a recipe: its title)."""
        if spec.id == "start-recipe":
            recipe = RECIPES.get(str(params.get("recipe")))
            return f"recipe {recipe.title if recipe else params.get('recipe')}"
        argv = self.argv(spec, params, build or self.stack.facts.build_id, tag)
        return " ".join([argv[0], *argv[2:]])

    def _kind_tls(self) -> str:
        return "0" if self.settings.profile == "kind-plaintext" else "1"

    def recipe_plan(self, recipe: Recipe, variant: str) -> list[dict[str, str]]:
        """The checklist for this profile and variant: the steps the body will walk."""
        profile = self.settings.profile
        fill = {
            "v": "" if variant == "live" else "-scripted",
            "tls": self._kind_tls(),
            "model": "bedrock" if variant == "live" else "scripted",
        }
        return [
            {"id": s.id, "label": s.label.format(**fill)}
            for s in recipe.steps
            if (s.profiles is None or profile in s.profiles)
            and (s.variants is None or variant in s.variants)
        ]

    def _recipe_why_not(self, recipe: Recipe) -> str | None:
        if self.settings.profile not in recipe.profiles:
            return f"Not on profile {self.settings.profile}."
        if recipe.needs_build and not self.stack.facts.build_id:
            return "No Build ID. Commit uv.lock, or start with TEMPORAL_BUILD_ID=<id>."
        if recipe.id == "kind-laptop" and self.stack.last and not self.stack.facts.temporal_up:
            return "Temporal on kind is not reachable: run Bring up kind first."
        return None

    def describe_recipes(self) -> list[dict[str, Any]]:
        out = []
        for r in RECIPES.values():
            why = self._recipe_why_not(r)
            out.append(
                {
                    "id": r.id,
                    "title": r.title,
                    "profiles": list(r.profiles),
                    "long": r.long,
                    "danger": r.danger,
                    "variants": ["scripted", "live"] if r.variants else [],
                    "steps": {
                        v: self.recipe_plan(r, v)
                        for v in (("scripted", "live") if r.variants else ("scripted",))
                    },
                    "available": why is None,
                    "why_not": why,
                }
            )
        return out

    def describe(self, action_id: str) -> dict[str, Any]:
        spec = SPECS[action_id]
        params = self._defaults(spec)
        why = self._why_not(spec, params)
        out = {
            "id": spec.id,
            "label": spec.label,
            "cost": self.cost(spec, params),
            "profiles": list(spec.profiles),
            "available": why is None,
            "why_not": why,
            "params": [
                {**p, "default": params.get(p["id"], p.get("default"))} for p in spec.params
            ],
            "make": self.make_text(spec, params),
            "long": spec.long,
            "group": spec.group,
        }
        if spec.id == "start-recipe":
            out["recipes"] = self.describe_recipes()
        return out

    def describe_all(self) -> list[dict[str, Any]]:
        return [self.describe(a) for a in SPECS]

    # --- running -----------------------------------------------------------------------------

    def _validate(self, spec: ActionSpec, raw: dict[str, Any]) -> dict[str, Any]:
        params = self._defaults(spec)
        for key, value in (raw or {}).items():
            schema = next((p for p in spec.params if p["id"] == key), None)
            if schema is None:
                raise ActionError("invalid_param", f"{spec.id} has no parameter {key!r}.")
            if schema["type"] == "choice" and value not in schema["choices"]:
                raise ActionError(
                    "invalid_param", f"{key} must be one of {schema['choices']}, got {value!r}."
                )
            if schema["type"] == "number":
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    raise ActionError("invalid_param", f"{key} must be a number.") from None
                if not schema["min"] <= value <= schema["max"]:
                    raise ActionError(
                        "invalid_param",
                        f"{key} must be {schema['min']}..{schema['max']}, got {value}.",
                    )
            params[key] = value
        if spec.id == "start-recipe":
            recipe = RECIPES[params["recipe"]]
            if self.settings.profile not in recipe.profiles:
                raise ActionError(
                    "precondition_failed",
                    f"{recipe.title} is not for profile {self.settings.profile}.",
                )
        return params

    def gate(self, cost: str, confirm: bool, *, long: bool = False, what: str = "") -> None:
        if (cost == "tokens" or long) and not confirm:
            note = {"tokens": "costs Bedrock tokens", "free": "takes several minutes"}[cost]
            raise ActionError(
                "confirm_required", f"{what or 'This action'} {note}.", "Send confirm: true."
            )

    def _busy(self, action_id: str) -> None:
        if action_id not in EXCLUSIVE:
            return
        for job in self.jobs.running():
            if job.action in EXCLUSIVE:
                what = job.title or job.command
                raise ActionError(
                    "precondition_failed",
                    f"{what} is still running.",
                    "One stack job runs at a time: wait for it to end (Manage stack, Jobs).",
                    job=job.id,
                )

    async def run(
        self, action_id: str, raw: dict[str, Any], confirm: bool
    ) -> tuple[int, dict[str, Any]]:
        spec = SPECS.get(action_id)
        if spec is None:
            raise ActionError("not_found", f"No action {action_id!r}.")
        if self.settings.profile not in spec.profiles:
            raise ActionError(
                "precondition_failed",
                f"{spec.label} is not available on profile {self.settings.profile}.",
            )
        params = self._validate(spec, raw)
        recipe = RECIPES[params["recipe"]] if spec.id == "start-recipe" else None
        title = recipe.title if recipe else spec.label
        cost = self.cost(spec, params)
        self.gate(cost, confirm, long=spec.long or bool(recipe and recipe.long), what=title)
        self._busy(spec.id)
        if (
            recipe
            and recipe.id == "kind-laptop"
            and not await self.http.tcp_open("127.0.0.1", 7233)
        ):
            raise ActionError(
                "precondition_failed",
                "Temporal on kind is not reachable: run Bring up kind first.",
                "Bring up kind starts the Docker VM's cluster, Temporal and the port-forward.",
            )
        needs_build = spec.needs_build or bool(recipe and recipe.needs_build)
        build = await self.stack.build_id() if needs_build else None
        if needs_build and not build:
            raise ActionError(
                "precondition_failed",
                "No Build ID. Commit uv.lock, or start with TEMPORAL_BUILD_ID=<id>.",
            )
        # Checked again with no await before the job exists: two quick clicks cannot both pass.
        self._busy(spec.id)
        job = await self._start(spec, params, cost, build, title)
        if spec.sync:
            try:
                job = await self.jobs.wait(job.id, within=30)
            except TimeoutError:
                return 202, self.jobs.view(job)  # still running: the stream reports the end
            return 200, self.jobs.view(job)
        return 202, self.jobs.view(job)

    async def _start(
        self, spec: ActionSpec, params: dict[str, Any], cost: str, build: str | None, title: str
    ) -> Job:
        tag = suffix()
        make = self.make_text(spec, params, build, tag)
        if not spec.in_process:
            return await self.jobs.run_command(
                spec.id,
                self.argv(spec, params, build, tag),
                env=self.settings.child_env(build),
                cost=cost,
                params=params,
                display=make,
                title=title,
                within=spec.within,
            )
        body: Callable[[JobLog], Awaitable[Any]]
        if spec.id == "invoke-local":
            body = lambda log: self.invoke_local(log, build)  # noqa: E731
        elif spec.id == "agentcore-ping":
            body = self._ping_body
        else:
            recipe, variant = params["recipe"], params["variant"]
            body = lambda log: self.recipe(log, recipe, variant)  # noqa: E731
        return await self.jobs.run_body(spec.id, make, body, cost=cost, params=params, title=title)

    # --- in-process bodies -------------------------------------------------------------------

    async def invoke_local(self, log: JobLog, build: str | None) -> dict[str, Any]:
        """``scripts/invoke-local.sh`` in-process: what the WCI sends, then ``/ping``."""
        url = self.settings.agentcore_url
        session = f"console-{int(time.time())}-{random.randrange(16**6):06x}"
        body = {"deploymentName": DEPLOYMENT_NAME, "buildId": build}
        log.line(f"POST {url}/invocations {body}")
        answer = await self.http.post(
            f"{url}/invocations",
            body,
            {"X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session},
        )
        if answer is None:
            raise RuntimeError(f"the AgentCore app is not answering on {url}")
        status, data = answer
        log.line(f"-> {status} {data}")
        ping = await self.http.get(f"{url}/ping")
        state = ping_status(ping[1]) if ping else None
        log.line(f"GET  {url}/ping -> {state}")
        if status != 200 or (isinstance(data, dict) and data.get("error")):
            err = data.get("error") if isinstance(data, dict) else data
            raise RuntimeError(f"the app refused: {err}")
        result = {
            "message": data.get("message") if isinstance(data, dict) else str(data),
            "lifetime": data.get("lifetime") if isinstance(data, dict) else None,
            "ping": state,
        }
        return result

    async def _ping_body(self, log: JobLog) -> dict[str, Any]:
        ping = await self.http.get(f"{self.settings.agentcore_url}/ping")
        state = ping_status(ping[1]) if ping else None
        log.line(f"/ping -> {state or 'not answering'}")
        return {"ping": state}

    # --- recipe steps (DESIGN.md 2.3) ----------------------------------------------------------

    async def _make(
        self, log: JobLog, *args: str, build: str | None = None, within: float = SHORT_S
    ) -> None:
        """``make -s <args>``. Steps longer than ``SHORT_S`` stream into the job log."""
        argv = ["make", "-s", *args]
        log.line("$ " + " ".join(argv[0:1] + argv[2:]))
        kind = self.settings.profile != "local"
        env = kind_env(self.settings, build) if kind else self.settings.child_env(build)
        if within > SHORT_S:
            code, output = await self._stream(log, argv, env, within)
        else:
            result = await self.procs.runner.run(
                argv, env=env, cwd=self.settings.root, within=within
            )
            code, output = result.returncode, result.output
            for line in output.splitlines()[-40:]:
                log.line("  " + line)
        if code == 0:
            return
        if code == 124:
            raise StepError(
                f"make {args[0]} ran longer than {within:g}s and was stopped",
                hint=hint_for(output, self.settings.profile),
                tail=meaningful(output)[-8:],
            )
        raise StepError(
            cause_line(output) or f"make {args[0]} exited {code}",
            hint=hint_for(output, self.settings.profile),
            tail=meaningful(output)[-8:],
        )

    async def _stream(
        self, log: JobLog, argv: list[str], env: dict[str, str], within: float
    ) -> tuple[int, str]:
        """Run with output appended to the job log as it comes; stop the group on timeout."""
        runner = self.procs.runner
        start = log.path.stat().st_size
        spawned = await runner.spawn(argv, env=env, cwd=self.settings.root, log_path=log.path)
        log.hold(spawned.pid)
        waiter = asyncio.ensure_future(spawned.wait())

        async def stop_group() -> None:
            runner.signal_group(spawned.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(asyncio.shield(waiter), 8)
            except TimeoutError:
                runner.signal_group(spawned.pid, signal.SIGKILL)
                await waiter

        loop = asyncio.get_running_loop()
        deadline = loop.time() + within
        size = start
        code: int | None = None
        try:
            while code is None:
                done, _ = await asyncio.wait({waiter}, timeout=0.5)
                now_size = log.path.stat().st_size
                if now_size != size:
                    size = now_size
                    log.touch()
                if done:
                    code = waiter.result()
                elif loop.time() > deadline:
                    await stop_group()
                    code = 124
        except asyncio.CancelledError:
            # The console is stopping: do not leave helm or kind running unwatched.
            if not waiter.done():
                await stop_group()
            raise
        log.hold(None)
        with log.path.open("rb") as f:
            f.seek(start)
            output = f.read().decode("utf-8", "replace")
        return code, output

    async def _ensure(
        self,
        log: JobLog,
        name: str,
        variant: str | None = None,
        wci: int = 0,
        build: str | None = None,
    ) -> None:
        if self.procs.running(name):
            have = self.procs.managed_variant(name)
            # Support's code changed since it started: the version this recipe registers is
            # ``build``, and a worker on another Build ID refuses the WCI's invocations.
            stale = self.procs.stale_build(name, build)
            if stale is None and (variant is None or have in (None, variant)):
                note = "" if have or variant is None else " (outside the console; variant unknown)"
                log.line(f"= {name} already running{note}")
                return
            if stale is not None:
                variant = variant or have
                log.line(f"~ restart {name}: Build ID {stale} -> {build}")
            else:
                # The recipe asked for the other model (scripted <-> live): restart it.
                log.line(f"~ restart {name}: {have} -> {variant}")
            try:
                await self.procs.stop(name)
            except ProcessError as err:
                raise StepError(f"{name}: {err.message}", err.hint) from err
        log.line(f"+ start {name}" + (f" ({variant})" if variant else ""))
        try:
            await self.procs.start(name, variant=variant, wci=wci, confirm=True)
        except ProcessError as err:
            raise StepError(f"{name}: {err.message}", err.hint) from err

    async def _stop(self, log: JobLog, name: str) -> None:
        if name in self.procs.records and self.procs.running(name):
            log.line(f"- stop {name}")
            await self.procs.stop(name)

    async def _wait_ping(self, log: JobLog, want: str | None, within: float = 90) -> str | None:
        deadline = asyncio.get_running_loop().time() + within
        state = None
        while asyncio.get_running_loop().time() < deadline:
            ping = await self.http.get(f"{self.settings.agentcore_url}/ping")
            state = ping_status(ping[1]) if ping else None
            if state is not None and (want is None or state == want):
                log.line(f"  /ping -> {state}")
                return state
            await asyncio.sleep(1.0)
        raise StepError(
            f"/ping did not answer {want or 'at all'} within {within:g}s",
            "Open the AgentCore app's log (Manage stack, Processes).",
        )

    async def _wait_temporal(self, log: JobLog, within: float = 60) -> None:
        gateway = self.stack.gateway
        if not gateway.connected:
            await gateway.close()  # forget the backoff from before the port-forward
        loop = asyncio.get_running_loop()
        deadline = loop.time() + within
        while True:
            if await gateway.ensure():
                try:
                    info = await gateway.system_info()
                except Exception:  # still starting
                    info = None
                if info is not None:
                    log.line(f"  Temporal answers: server {info.get('server_version') or '?'}")
                    return
            if loop.time() >= deadline:
                break
            await asyncio.sleep(2.0)
        error = gateway.last_error or "no answer"
        raise StepError(
            f"Temporal on kind did not answer on 127.0.0.1:7233 within {within:g}s ({error})",
            hint_for(error, self.settings.profile)
            or "The frontend may still be starting: run Bring up kind again in a minute.",
        )

    async def _finance_polling(self) -> bool:
        try:
            pollers = await self.stack.gateway.pollers(SERVICES_NS, FINANCE_TQ)
        except Exception:
            return False
        return bool(fresh(pollers))

    # --- recipes -------------------------------------------------------------------------------

    async def recipe(self, log: JobLog, recipe_id: str, variant: str) -> dict[str, Any]:
        recipe = RECIPES[recipe_id]
        if not recipe.variants:
            variant = "scripted"
        log.plan(self.recipe_plan(recipe, variant))
        build = await self.stack.build_id() if recipe.needs_build else None
        if recipe.needs_build and not build:
            raise RuntimeError("No Build ID. Commit uv.lock, or start with TEMPORAL_BUILD_ID=<id>.")
        try:
            if recipe_id == "teardown":
                return await self._teardown(log)
            if recipe_id == "kind-up":
                await self._kind_cluster(log, variant)
            if recipe_id == "kind-laptop":
                with log.step("temporal"):
                    if not await self.http.tcp_open("127.0.0.1", 7233):
                        raise StepError(
                            "Temporal on kind is not reachable: run Bring up kind first",
                            "Bring up kind (Manage stack, Stack recipes).",
                        )
            if recipe_id in KIND_RECIPES:
                await self._laptop_side(log, variant, build)
            else:
                await self._local(log, recipe_id, variant, build)
        finally:
            if recipe_id in KIND_RECIPES:
                self.stack.kind.invalidate()
        return {"recipe": recipe_id, "steps": len(log.steps)}

    async def _teardown(self, log: JobLog) -> dict[str, Any]:
        with log.step("stop"):
            stopped = await self.procs.stop_all()
            for name in stopped:
                log.line(f"- stopped {name}")
        with log.step("dev-server"):
            if self.procs.running("dev-server"):
                log.line("- stop dev-server")
                await self.procs.stop("dev-server")
                stopped.append("dev-server")
            else:
                log.skip("dev-server", "not running")
        return {"recipe": "teardown", "stopped": stopped}

    async def _local(self, log: JobLog, recipe: str, variant: str, build: str | None) -> None:
        if recipe == "local-wci":
            with log.step("drain"):
                if self.procs.running("agentcore-local"):
                    await self._wait_ping(log, "Healthy", within=180)
                else:
                    log.skip("drain", "the AgentCore app is not running")
            with log.step("dev-server"):
                if self.procs.dev_server_wci() not in (None, "1"):
                    log.line("- restart the dev server with WCI=1")
                    await self.procs.stop("dev-server")
                await self._ensure(log, "dev-server", wci=1)
        else:
            with log.step("dev-server"):
                await self._ensure(log, "dev-server", wci=0)
        with log.step("stop"):
            if recipe == "local-plain":
                await self._stop(log, "agentcore-local")
                await self._stop(log, "agentcore-emulator")
            else:
                await self._stop(log, "worker-support")
        with log.step("payments-mock"):
            await self._ensure(log, "payments-mock")
        with log.step("worker-finance"):
            await self._ensure(log, "worker-finance", variant)
        with log.step("nexus-endpoint"):
            await self._make(log, "nexus-endpoint")
        if recipe == "local-plain":
            with log.step("worker-support"):
                await self._ensure(log, "worker-support", variant)
            return
        if recipe == "local-wci":
            with log.step("agentcore-emulator"):
                await self._ensure(log, "agentcore-emulator")
        with log.step("agentcore-local"):
            await self._ensure(log, "agentcore-local", variant, build=build)
        with log.step("ping"):
            await self._wait_ping(log, None)
        if recipe == "local-invoke":
            with log.step("invoke-local"):
                await self.invoke_local(log, build)
            with log.step("register-version"):
                await self._make(log, "register-version", "MODE=--poller", build=build)
        else:
            with log.step("register-version"):
                await self._make(log, "register-version", "MODE=--local-wci", build=build)

    async def _kind_cluster(self, log: JobLog, variant: str) -> None:
        """Docker VM check, then each cluster layer unless it is already there."""
        runner, s = self.procs.runner, self.settings
        tls = self._kind_tls()
        with log.step("docker"):
            if not await docker_up(runner, s):
                raise StepError(
                    f"Docker VM {COLIMA_PROFILE} is stopped",
                    "Start Docker VM (Manage stack), then run Bring up kind again.",
                )
            log.line(f"  docker context colima-{COLIMA_PROFILE} answers")
        with log.step("kind-up"):
            if await cluster_exists(runner, s) and await postgres_ready(runner, s):
                log.skip("kind-up", f"cluster {KIND_CLUSTER} is up")
            else:
                await self._make(log, "kind-up", within=900)
        with log.step("kind-temporal"):
            status, frontend_tls = await helm_release(runner, s)
            want = tls == "1"
            word = {True: "on", False: "off", None: "unknown"}
            if status == "deployed" and frontend_tls == want:
                log.skip("kind-temporal", f"release temporal is deployed (TLS {word[want]})")
            else:
                if status == "deployed":
                    log.line(f"~ release temporal has TLS {word[frontend_tls]}; reinstalling")
                elif status:
                    log.line(f"~ release temporal is {status}; installing again")
                await self._make(log, "kind-temporal", f"KIND_TLS={tls}", within=1500)
        if any(st["id"] == "certs-pull" for st in log.steps):
            with log.step("certs-pull"):
                await self._make(log, "certs-pull", within=120)
            # The stack snapshot recomputes this only on its next refresh; agentcore-local
            # checks it at start, seconds from now.
            self.stack.kind.invalidate()
            self.procs.certs_state = "ok"
        with log.step("pf"):
            if self.stack.kind.port_forward() and await self.http.tcp_open("127.0.0.1", 7233):
                log.line("= port-forward already running")
            else:
                await self._make(log, "pf", within=120)
            self.stack.kind.invalidate()
            await self._wait_temporal(log, within=60)
        if variant == "live":
            with log.step("kind-aws-creds"):
                await self._make(log, "kind-aws-creds", within=120)
        with log.step("kind-workers"):
            model = "bedrock" if variant == "live" else "scripted"
            image = await finance_image(runner, s)
            want_tag = ":kind" if model == "bedrock" else ":kind-scripted"
            if image and image.endswith(want_tag) and await self._finance_polling():
                log.skip("kind-workers", f"Finance pods poll agent-services-tq ({model})")
            else:
                await self._make(
                    log, "kind-workers", f"FINANCE_MODEL={model}", f"KIND_TLS={tls}", within=1200
                )

    async def _laptop_side(self, log: JobLog, variant: str, build: str | None) -> None:
        with log.step("nexus-endpoint"):
            await self._make(log, "nexus-endpoint")
        with log.step("agentcore-local"):
            await self._ensure(log, "agentcore-local", variant, build=build)
        with log.step("ping"):
            await self._wait_ping(log, None)
        with log.step("invoke-local"):
            await self.invoke_local(log, build)
        with log.step("register-version"):
            await self._make(log, "register-version", "MODE=--poller", build=build)


KIND_RECIPES = frozenset(r.id for r in RECIPES.values() if set(r.profiles) <= set(KIND))
