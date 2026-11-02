"""Actions: one-shot jobs, recipes and the cost gate (DESIGN.md 2.3, 4.5, 5.5).

Every action has a cost (``free`` | ``tokens`` | ``aws``). ``tokens`` needs ``confirm: true``;
``aws`` also needs the console to run with ``CONSOLE_ALLOW_AWS=1`` (else ``403 aws_disabled``).
Long kind lifecycle jobs need ``confirm`` too. Commands are ``make -s <target>`` so the Makefile
stays the source of truth; invoke-local and the ping are done in-process (plain HTTP).
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from demo_console.cloud import AwsDisabled, CloudAdapter, failure_argv
from demo_console.jobs import Job, JobLog, JobStore
from demo_console.procs import ProcessError, ProcessManager
from demo_console.scenarios import suffix
from demo_console.settings import DEPLOYMENT_NAME, Settings
from demo_console.stack import HttpProbe, StackProbe, ping_status

KIND = ("kind", "kind-plaintext")
ALL = ("local", "kind", "kind-plaintext", "eks-pf")
RECIPES = {
    "local-plain": ("local",),
    "local-invoke": ("local",),
    "local-wci": ("local",),
    "kind-laptop": KIND,
    "teardown": ("local",),
}


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
        ),
        ActionSpec("nexus-endpoint", "Create the Nexus endpoint finance-agent", ALL),
        ActionSpec(
            "register-version",
            "Register Support's version",
            ALL,
            [
                {
                    "id": "mode",
                    "type": "choice",
                    "choices": ["--poller", "--local-wci", "--agentcore-auto"],
                    "default": "--poller",
                }
            ],
            needs_build=True,
        ),
        ActionSpec("invoke-local", "Invoke AgentCore app", ALL, needs_build=True, sync=True),
        ActionSpec("agentcore-ping", "Ping the AgentCore app", ALL, sync=True),
        ActionSpec("smoke", "Run the smoke caller", ("local",)),
        ActionSpec("card", "Standalone get_agent_card", ALL),
        ActionSpec(
            "load",
            "Start N conversations",
            ("local", *KIND, "eks-pf"),
            [
                {"id": "n", "type": "number", "default": 10, "min": 1, "max": 20},
                {"id": "kind", "type": "choice", "choices": ["ping", "case-a"], "default": "ping"},
            ],
        ),
        ActionSpec("wci-history", "Show the WCI's history", ALL, needs_build=True),
        ActionSpec(
            "failure",
            "Failure demo on EKS (scale Finance to 0 and back)",
            ("eks-pf",),
            [{"id": "workflow_id", "type": "text", "default": None}],
        ),
        ActionSpec(
            "drain-cloudwatch",
            "Confirm the drain in CloudWatch",
            ("eks-pf",),
            [{"id": "workflow_id", "type": "text", "default": None}],
            needs_build=True,
        ),
        ActionSpec("kind-up", "Create the kind cluster", KIND, long=True),
        ActionSpec("kind-temporal", "Install Temporal on kind", KIND, long=True),
        ActionSpec("kind-workers", "Finance + payments pods (scripted)", KIND, long=True),
        ActionSpec("certs-pull", "Pull client certificates", (*KIND, "eks-pf"), long=True),
        ActionSpec("pf", "Port-forward Temporal", (*KIND, "eks-pf"), long=True),
        ActionSpec("pf-stop", "Stop the port-forwards", (*KIND, "eks-pf"), long=True),
        ActionSpec("kind-down", "Delete the kind cluster", KIND, long=True),
    )
}


class ActionService:
    def __init__(
        self,
        settings: Settings,
        procs: ProcessManager,
        jobs: JobStore,
        stack: StackProbe,
        http: HttpProbe,
        cloud: CloudAdapter,
        *,
        on_cloudwatch: Callable[[str, dict[str, Any]], None] | None = None,
        run_started_at: Callable[[str], Awaitable[float | None]] | None = None,
    ) -> None:
        self.settings = settings
        self.procs = procs
        self.jobs = jobs
        self.stack = stack
        self.http = http
        self.cloud = cloud
        self._on_cloudwatch = on_cloudwatch
        self._run_started_at = run_started_at

    # --- descriptions ------------------------------------------------------------------------

    def _model_live(self) -> bool:
        snap = self.stack.last or {}
        return (snap.get("model") or {}).get("mode") == "live"

    def cost(self, spec: ActionSpec, params: dict[str, Any]) -> str:
        p = self.settings.profile
        if spec.id in ("failure", "drain-cloudwatch"):
            return "aws"
        if p == "eks-pf" and spec.id in ("certs-pull", "pf", "pf-stop", "load"):
            return "aws"
        if spec.id == "register-version" and params.get("mode") == "--agentcore-auto":
            return "aws"
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
            out["recipe"] = "kind-laptop" if self.settings.profile in KIND else "local-plain"
        return out

    def _default_mode(self) -> str:
        if self.settings.profile == "eks-pf":
            return "--agentcore-auto"
        snap = self.stack.last or {}
        if (snap.get("wci") or {}).get("mode") == "emulator":
            return "--local-wci"
        return "--poller"

    def _why_not(self, spec: ActionSpec, params: dict[str, Any]) -> str | None:
        if self.settings.profile not in spec.profiles:
            return f"Not on profile {self.settings.profile}."
        if spec.needs_build and not self.stack.facts.build_id:
            return "No Build ID. Commit uv.lock, or start with TEMPORAL_BUILD_ID=<id>."
        if self.cost(spec, params) == "aws" and not self.settings.allow_aws:
            return "Calls AWS: start the console with CONSOLE_ALLOW_AWS=1."
        return None

    def make_text(self, spec: ActionSpec, params: dict[str, Any]) -> str:
        p = params
        texts = {
            "start-recipe": f"(recipe {p.get('recipe')}: see the job log for each make target)",
            "register-version": f"make register-version MODE={p.get('mode')}"
            + (" CLOUD=1" if p.get("mode") == "--agentcore-auto" else ""),
            "load": f"make load N={p.get('n')} LOAD_KIND={p.get('kind')}",
            "wci-history": f"make wci-history BUILD={self.stack.facts.build_id or '<build>'}",
            "invoke-local": "TEMPORAL_BUILD_ID="
            f"{self.stack.facts.build_id or '<build>'} make invoke-local",
            "failure": f"make failure WF={p.get('workflow_id') or '<wf>'}",
            "drain-cloudwatch": "uv run python -m demo_testing.agentcore_logs wait-drained "
            f"{p.get('workflow_id') or '<wf>'}",
            "kind-temporal": f"make kind-temporal KIND_TLS={self._kind_tls()}",
            "kind-workers": "make kind-workers FINANCE_MODEL=scripted",
            "smoke": "make smoke",
        }
        text = texts.get(spec.id, f"make {spec.id}")
        if self.settings.profile != "local" and text.startswith("make "):
            text += f" TEMPORAL_PROFILE={self.settings.profile}"
        return text

    def _kind_tls(self) -> str:
        return "0" if self.settings.profile == "kind-plaintext" else "1"

    def describe(self, action_id: str) -> dict[str, Any]:
        spec = SPECS[action_id]
        params = self._defaults(spec)
        why = self._why_not(spec, params)
        return {
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
        }

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
        if spec.id == "start-recipe" and self.settings.profile not in RECIPES[params["recipe"]]:
            raise ActionError(
                "precondition_failed",
                f"Recipe {params['recipe']} is not for profile {self.settings.profile}.",
            )
        return params

    def gate(self, cost: str, confirm: bool, *, long: bool = False, what: str = "") -> None:
        if cost == "aws" and not self.settings.allow_aws:
            raise ActionError(
                "aws_disabled",
                f"{what or 'This action'} calls AWS.",
                "Start the console with CONSOLE_ALLOW_AWS=1 to allow AWS actions.",
            )
        if (cost in ("tokens", "aws") or long) and not confirm:
            note = {
                "tokens": "costs Bedrock tokens",
                "aws": "calls AWS",
                "free": "takes several minutes",
            }[cost]
            raise ActionError(
                "confirm_required", f"{what or 'This action'} {note}.", "Send confirm: true."
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
        cost = self.cost(spec, params)
        self.gate(cost, confirm, long=spec.long, what=spec.label)
        build = await self.stack.build_id() if spec.needs_build else None
        if spec.needs_build and not build:
            raise ActionError(
                "precondition_failed",
                "No Build ID. Commit uv.lock, or start with TEMPORAL_BUILD_ID=<id>.",
            )
        job = await self._start(spec, params, cost, build)
        if spec.sync:
            job = await self.jobs.wait(job.id, within=30)
            return 200, self.jobs.view(job)
        return 202, self.jobs.view(job)

    async def _start(
        self, spec: ActionSpec, params: dict[str, Any], cost: str, build: str | None
    ) -> Job:
        env = self.settings.child_env(build)
        make = self.make_text(spec, params)

        def cmd(*argv: str, extra: dict[str, str] | None = None) -> Awaitable[Job]:
            return self.jobs.run_command(
                spec.id,
                list(argv),
                env={**env, **(extra or {})},
                cost=cost,
                params=params,
                display=make,
            )

        match spec.id:
            case "nexus-endpoint":
                return await cmd("make", "-s", "nexus-endpoint")
            case "register-version":
                argv = ["make", "-s", "register-version", f"MODE={params['mode']}"]
                if params["mode"] == "--agentcore-auto":
                    argv.append("CLOUD=1")
                return await cmd(*argv)
            case "smoke":
                return await cmd("make", "-s", "smoke", f"WF=smoke-1042-{suffix()}")
            case "card":
                return await cmd("make", "-s", "card")
            case "load":
                return await cmd(
                    "make", "-s", "load", f"N={params['n']}", extra={"LOAD_KIND": params["kind"]}
                )
            case "wci-history":
                return await cmd("make", "-s", "wci-history", f"BUILD={build}")
            case "failure":
                wf = params.get("workflow_id") or f"support-1042-failure-{suffix()}"
                return await cmd(*failure_argv(wf))
            case "kind-up" | "kind-down" | "certs-pull" | "pf" | "pf-stop":
                return await cmd("make", "-s", spec.id)
            case "kind-temporal":
                return await cmd("make", "-s", "kind-temporal", f"KIND_TLS={self._kind_tls()}")
            case "kind-workers":
                return await cmd(
                    "make",
                    "-s",
                    "kind-workers",
                    "FINANCE_MODEL=scripted",
                    f"KIND_TLS={self._kind_tls()}",
                )
            case "invoke-local":
                return await self.jobs.run_body(
                    spec.id, make, lambda log: self.invoke_local(log, build), cost=cost
                )
            case "agentcore-ping":
                return await self.jobs.run_body(
                    spec.id, "make agentcore-ping", self._ping_body, cost=cost
                )
            case "drain-cloudwatch":
                wf = params.get("workflow_id")
                if not wf:
                    raise ActionError("invalid_param", "drain-cloudwatch needs workflow_id.")
                return await self.jobs.run_body(
                    spec.id, make, lambda log: self._drain(log, wf, build), cost=cost, params=params
                )
            case "start-recipe":
                recipe = params["recipe"]
                variant = params["variant"]
                return await self.jobs.run_body(
                    spec.id,
                    f"recipe {recipe}",
                    lambda log: self.recipe(log, recipe, variant),
                    cost=cost,
                    params=params,
                )
        raise ActionError("not_found", f"No action {spec.id!r}.")

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

    async def _drain(self, log: JobLog, wf: str, build: str | None) -> dict[str, Any]:
        since = await self._run_started_at(wf) if self._run_started_at else None
        since_ms = int(((since or time.time() - 3600) - 5) * 1000)
        log.line(f"waiting for {wf}'s AgentCore sessions to drain (CloudWatch)")
        try:
            sessions = await self.cloud.wait_drained(wf, since_ms, build)
        except AwsDisabled as err:
            raise RuntimeError(str(err)) from err
        released = [s["released_iso"] for s in sessions if s.get("released_iso")]
        result = {"sessions": sessions, "drained_at": max(released) if released else None}
        for s in sessions:
            log.line(f"session {s['session']}: lifetimes {s['lifetimes']}, drained {s['drained']}")
        if self._on_cloudwatch:
            self._on_cloudwatch(wf, result)
        return result

    # --- recipes (DESIGN.md 2.3) ---------------------------------------------------------------

    async def _make(self, log: JobLog, *args: str, build: str | None = None) -> None:
        argv = ["make", "-s", *args]
        log.line("$ " + " ".join(argv[0:1] + argv[2:]))
        result = await self.procs.runner.run(
            argv, env=self.settings.child_env(build), cwd=self.settings.root, within=180
        )
        for line in result.output.splitlines()[-40:]:
            log.line("  " + line)
        if result.returncode != 0:
            raise RuntimeError(f"make {args[0]} exited {result.returncode}")

    async def _ensure(
        self, log: JobLog, name: str, variant: str | None = None, wci: int = 0
    ) -> None:
        if self.procs.running(name):
            have = self.procs.managed_variant(name)
            if variant is None or have in (None, variant):
                note = "" if have or variant is None else " (outside the console; variant unknown)"
                log.line(f"= {name} already running{note}")
                return
            # The recipe asked for the other model (scripted <-> live): restart it.
            log.line(f"~ restart {name}: {have} -> {variant}")
            try:
                await self.procs.stop(name)
            except ProcessError as err:
                raise RuntimeError(f"{name}: {err.message}") from err
        log.line(f"+ start {name}" + (f" ({variant})" if variant else ""))
        try:
            await self.procs.start(name, variant=variant, wci=wci, confirm=True)
        except ProcessError as err:
            raise RuntimeError(f"{name}: {err.message}") from err

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
        raise RuntimeError(f"/ping did not answer {want or 'at all'} within {within:g}s")

    async def recipe(self, log: JobLog, recipe: str, variant: str) -> dict[str, Any]:
        build = await self.stack.build_id()
        if recipe in ("local-invoke", "local-wci", "kind-laptop") and not build:
            raise RuntimeError("No Build ID. Commit uv.lock, or start with TEMPORAL_BUILD_ID=<id>.")
        if recipe == "teardown":
            stopped = await self.procs.stop_all()
            for name in stopped:
                log.line(f"- stopped {name}")
            if self.procs.running("dev-server"):
                log.line("- stop dev-server")
                await self.procs.stop("dev-server")
                stopped.append("dev-server")
            return {"stopped": stopped}
        if recipe == "kind-laptop":
            await self._make(log, "nexus-endpoint")
            await self._ensure(log, "agentcore-local", variant)
            await self._wait_ping(log, None)
            await self.invoke_local(log, build)
            await self._make(log, "register-version", "MODE=--poller", build=build)
            return {"recipe": recipe}
        if recipe == "local-wci":
            if self.procs.running("agentcore-local"):
                log.line("waiting for /ping Healthy before restarting the dev server")
                await self._wait_ping(log, "Healthy", within=180)
            if self.procs.dev_server_wci() not in (None, "1"):
                log.line("- restart the dev server with WCI=1")
                await self.procs.stop("dev-server")
            await self._ensure(log, "dev-server", wci=1)
        else:
            await self._ensure(log, "dev-server", wci=0)
        if recipe == "local-plain":
            await self._stop(log, "agentcore-local")
            await self._stop(log, "agentcore-emulator")
        else:
            await self._stop(log, "worker-support")
        await self._ensure(log, "payments-mock")
        await self._ensure(log, "worker-finance", variant)
        await self._make(log, "nexus-endpoint")
        if recipe == "local-plain":
            await self._ensure(log, "worker-support", variant)
            return {"recipe": recipe}
        if recipe == "local-wci":
            await self._ensure(log, "agentcore-emulator")
        await self._ensure(log, "agentcore-local", variant)
        await self._wait_ping(log, None)
        if recipe == "local-invoke":
            await self.invoke_local(log, build)
            await self._make(log, "register-version", "MODE=--poller", build=build)
        else:
            await self._make(log, "register-version", "MODE=--local-wci", build=build)
        return {"recipe": recipe}
