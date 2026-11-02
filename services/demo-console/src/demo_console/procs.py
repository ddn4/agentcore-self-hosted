"""Laptop processes the console may start and stop (DESIGN.md 4.5).

Each one is ``make -s <target>`` (the Makefile stays the single source of truth for args and
per-identity client certs), spawned in its own session and recorded under
``.temporal/console/procs/<name>.json``; on startup every record whose process group is still
alive is adopted, so a console restart loses nothing. Processes started elsewhere show up as
``external`` (their port answers or their task queue has pollers) and are never stopped.
Never cluster or AWS resources: on kind and eks-pf only the laptop-side Support processes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from demo_console.events import EventBus
from demo_console.payloads import iso, now
from demo_console.runner import CommandRunner, Spawned
from demo_console.settings import (
    AGENTS_NS,
    DEV_TQ,
    FINANCE_TQ,
    SERVICES_NS,
    SMOKE_TQ,
    VERSIONED_TQ,
    Settings,
)

ALL_PROFILES = ("local", "kind", "kind-plaintext", "eks-pf")
STOP_GRACE_S = 8.0


class ProcessError(Exception):
    """A start/stop the console refuses; ``code`` maps to the API error (DESIGN.md 5.0)."""

    def __init__(self, code: str, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


@dataclass(frozen=True)
class ProcSpec:
    name: str
    label: str
    #: variant -> make target (``default`` when there is only one).
    targets: dict[str, str]
    ports: tuple[int, ...] = ()
    profiles: tuple[str, ...] = ("local",)
    needs: tuple[str, ...] = ()
    #: ``namespace/task-queue`` whose pollers reveal an external instance.
    polls: str | None = None
    #: Per-profile client certificate directory the make target uses (kind / eks-pf).
    certs: dict[str, str] = field(default_factory=dict)

    @property
    def variants(self) -> list[str]:
        return list(self.targets)

    def default_variant(self) -> str:
        return "scripted" if "scripted" in self.targets else next(iter(self.targets))


SPECS: dict[str, ProcSpec] = {
    spec.name: spec
    for spec in (
        ProcSpec(
            "dev-server",
            "Temporal dev server",
            {"default": "dev-server-up"},
            ports=(7233, 8233),
        ),
        ProcSpec("payments-mock", "Payments mock", {"default": "payments-mock"}, ports=(8081,)),
        ProcSpec(
            "worker-finance",
            "Finance worker",
            {"scripted": "worker-finance-scripted", "live": "worker-finance"},
            polls=f"{SERVICES_NS}/{FINANCE_TQ}",
        ),
        ProcSpec(
            "worker-support",
            "Support worker (plain)",
            {"scripted": "worker-support-scripted", "live": "worker-support"},
            profiles=ALL_PROFILES,
            polls=f"{AGENTS_NS}/{DEV_TQ}",
            certs={"kind": ".certs/laptop-support", "eks-pf": ".certs/eks/laptop-support"},
        ),
        ProcSpec(
            "worker-support-versioned",
            "Support worker (versioned, long-lived)",
            {"live": "worker-support-versioned"},
            profiles=ALL_PROFILES,
            needs=("build_id",),
            polls=f"{AGENTS_NS}/{VERSIONED_TQ}",
            certs={"kind": ".certs/agentcore-worker", "eks-pf": ".certs/eks/agentcore-worker"},
        ),
        ProcSpec(
            "agentcore-local",
            "Local AgentCore app",
            {"scripted": "agentcore-local-scripted", "live": "agentcore-local"},
            ports=(8080,),
            profiles=ALL_PROFILES,
            needs=("build_id",),
            certs={"kind": ".certs/agentcore-worker", "eks-pf": ".certs/eks/agentcore-worker"},
        ),
        ProcSpec(
            "agentcore-emulator",
            "WCI emulator (AgentCore API)",
            {"default": "agentcore-emulator"},
            ports=(4599,),
        ),
        ProcSpec(
            "worker-smoke",
            "Smoke caller worker",
            {"default": "worker-smoke"},
            polls=f"{AGENTS_NS}/{SMOKE_TQ}",
        ),
    )
}


@dataclass
class ProcRecord:
    name: str
    pid: int
    pgid: int
    target: str
    variant: str
    profile: str
    started_at: str
    wci: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "pid": self.pid,
            "pgid": self.pgid,
            "target": self.target,
            "variant": self.variant,
            "profile": self.profile,
            "started_at": self.started_at,
            "wci": self.wci,
        }


@dataclass
class _Last:
    """What the console remembers about a process that is no longer running."""

    exit_code: int | None
    ended_at: str
    variant: str
    #: True when the console stopped it (status `stopped`), False when it exited on its own.
    stopped: bool = False


class ProcessManager:
    def __init__(
        self,
        settings: Settings,
        runner: CommandRunner,
        bus: EventBus | None = None,
        *,
        build_id_provider: Callable[[], Awaitable[str | None]] | None = None,
    ) -> None:
        self.settings = settings
        self.runner = runner
        self.bus = bus or EventBus()
        #: The resolved Build ID (DESIGN.md 4.4); the stack probe provides it.
        self.build_id_provider = build_id_provider
        self.records: dict[str, ProcRecord] = {}
        self._waiters: dict[str, asyncio.Task[None]] = {}
        self._stopping: set[str] = set()
        self._last: dict[str, _Last] = {}
        self._monitor: asyncio.Task[None] | None = None
        #: External facts from the last stack probe: open ports and polled queues.
        self.ports_open: set[int] = set()
        self.polled: set[str] = set()
        #: The Build ID the last stack probe resolved (``known`` once it ran).
        self.build_id_hint: str | None = None
        self.build_id_known = False
        self.proc_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    # --- paths ---------------------------------------------------------------------------

    @property
    def proc_dir(self) -> Path:
        return self.settings.state_dir / "procs"

    @property
    def log_dir(self) -> Path:
        return self.settings.state_dir / "logs"

    def log_path(self, name: str) -> Path:
        return self.log_dir / f"{name}.log"

    @property
    def kills_path(self) -> Path:
        return self.settings.state_dir / "kills.jsonl"

    @property
    def exits_path(self) -> Path:
        return self.settings.state_dir / "exits.jsonl"

    # --- adoption ------------------------------------------------------------------------

    def adopt(self) -> list[str]:
        """Adopt every record whose process group is alive; drop the rest."""
        adopted: list[str] = []
        for path in sorted(self.proc_dir.glob("*.json")):
            try:
                rec = ProcRecord(**json.loads(path.read_text()))
            except (ValueError, TypeError):
                path.unlink(missing_ok=True)
                continue
            if rec.name == "dev-server":
                alive = self._dev_server_pid() is not None
            else:
                alive = self.runner.group_alive(rec.pgid)
            if alive and rec.name in SPECS:
                self.records[rec.name] = rec
                adopted.append(rec.name)
                self.bus.publish("process", self.describe(rec.name))
            else:
                path.unlink(missing_ok=True)
        return adopted

    def start_monitor(self) -> None:
        if self._monitor is None or self._monitor.done():
            self._monitor = asyncio.create_task(self._watch())

    async def stop_monitor(self) -> None:
        if self._monitor is not None:
            self._monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._monitor
            self._monitor = None

    async def _watch(self) -> None:
        """Adopted processes have no waiter: notice when they go away."""
        while True:
            await asyncio.sleep(2.0)
            for name, rec in list(self.records.items()):
                if name in self._waiters:
                    continue
                if not self._alive(rec):
                    self._forget(name, exit_code=None)

    # --- status --------------------------------------------------------------------------

    def _dev_server_pid(self) -> int | None:
        path = self.settings.temporal_dir / "dev-server.pid"
        try:
            pid = int(path.read_text().strip())
        except (OSError, ValueError):
            return None
        return pid if self.runner.pid_alive(pid) else None

    def _alive(self, rec: ProcRecord) -> bool:
        if rec.name == "dev-server":
            return self._dev_server_pid() is not None
        return self.runner.group_alive(rec.pgid)

    def _unavailable(self, spec: ProcSpec, build_id: str | None) -> str | None:
        profile = self.settings.profile
        if profile not in spec.profiles:
            return f"Not on profile {profile}: the console only runs laptop processes there."
        if "build_id" in spec.needs and not build_id:
            return "No Build ID. Commit uv.lock, or start with TEMPORAL_BUILD_ID=<id>."
        cert_dir = spec.certs.get(profile)
        if cert_dir and not (self.settings.root / cert_dir / "tls.crt").is_file():
            pull = "make certs-pull" + (" TEMPORAL_PROFILE=eks-pf" if profile == "eks-pf" else "")
            return f"No client certificate in {cert_dir}/ ({pull})."
        return None

    def _external(self, spec: ProcSpec) -> bool:
        if spec.name == "dev-server":
            return self._dev_server_pid() is not None
        if any(port in self.ports_open for port in spec.ports):
            return True
        return bool(spec.polls and spec.polls in self.polled)

    def describe(self, name: str) -> dict[str, Any]:
        """``Process`` (DESIGN.md 5.5)."""
        spec = SPECS[name]
        rec = self.records.get(name)
        last = self._last.get(name)
        variant = rec.variant if rec else (last.variant if last else spec.default_variant())
        target = spec.targets.get(variant, next(iter(spec.targets.values())))
        status: str
        why: str | None = None
        if rec is not None and self._alive(rec):
            status = "running"
        elif self._external(spec):
            status = "external"
        else:
            # Until a stack probe resolved the Build ID, do not claim it is missing.
            hint = self.build_id_hint if self.build_id_known else "unknown"
            why = self._unavailable(spec, hint)
            status = (
                "unavailable" if why else ("exited" if last and not last.stopped else "stopped")
            )
        make = f"make {target}"
        if name == "dev-server":
            wci = rec.wci if rec and rec.wci is not None else 0
            make = f"make dev-server-up WCI={wci}"
        body: dict[str, Any] = {
            "name": name,
            "label": spec.label,
            "variant": variant,
            "variants": spec.variants,
            "status": status,
            "pid": rec.pid if rec else None,
            "started_at": rec.started_at if rec else None,
            "exit_code": last.exit_code if (last and rec is None) else None,
            "make": make,
            "cost": "tokens" if variant == "live" else "free",
            "ports": list(spec.ports),
            "profiles": list(spec.profiles),
            "needs": list(spec.needs),
            "managed": rec is not None,
        }
        if why:
            body["why"] = why
        if name == "dev-server":
            body["wci"] = self.dev_server_wci()
        return body

    def describe_all(self) -> list[dict[str, Any]]:
        return [self.describe(name) for name in SPECS]

    def dev_server_wci(self) -> str | None:
        """``.temporal/dev-server.wci`` while the dev server runs: ``0``, ``1``, ``aws``."""
        if self._dev_server_pid() is None:
            return None
        try:
            return (self.settings.temporal_dir / "dev-server.wci").read_text().strip() or "0"
        except OSError:
            return "0"

    def running(self, name: str) -> bool:
        rec = self.records.get(name)
        if rec is not None and self._alive(rec):
            return True
        return self._external(SPECS[name])

    def managed_variant(self, name: str) -> str | None:
        rec = self.records.get(name)
        return rec.variant if rec is not None and self._alive(rec) else None

    def other_profile(self, profile: str) -> list[str]:
        """Managed processes started under another profile and still running."""
        return [
            name
            for name, rec in self.records.items()
            if rec.profile != profile and self._alive(rec)
        ]

    def kills(self) -> list[dict[str, Any]]:
        return self._jsonl(self.kills_path)

    def exits(self, name: str) -> list[dict[str, Any]]:
        """``{name, at, started_at}`` for each time the managed process ``name`` ended."""
        return [e for e in self._jsonl(self.exits_path) if e.get("name") == name]

    @staticmethod
    def _jsonl(path: Path) -> list[dict[str, Any]]:
        try:
            lines = path.read_text().splitlines()
        except OSError:
            return []
        out: list[dict[str, Any]] = []
        for line in lines:
            with contextlib.suppress(ValueError):
                out.append(json.loads(line))
        return out

    # --- start / stop ----------------------------------------------------------------------

    async def _resolve_build_id(self) -> str | None:
        if self.build_id_provider is None:
            return None
        return await self.build_id_provider()

    async def start(
        self, name: str, *, variant: str | None = None, wci: int = 0, confirm: bool = False
    ) -> dict[str, Any]:
        spec = SPECS.get(name)
        if spec is None:
            raise ProcessError("not_found", f"No process {name!r}.")
        variant = variant or spec.default_variant()
        if variant not in spec.targets:
            raise ProcessError(
                "invalid_param", f"{name} has no variant {variant!r}; use one of {spec.variants}."
            )
        build_id = await self._resolve_build_id() if "build_id" in spec.needs else None
        why = self._unavailable(spec, build_id if "build_id" in spec.needs else "n/a")
        if why:
            raise ProcessError("precondition_failed", why)
        if variant == "live" and not confirm:
            raise ProcessError(
                "confirm_required",
                f"{spec.label} ({variant}) calls Bedrock and costs tokens.",
                "Send confirm: true, or pick the scripted variant.",
            )
        if self.running(name):
            raise ProcessError("precondition_failed", f"{spec.label} is already running.")
        if name == "dev-server":
            if wci not in (0, 1):
                raise ProcessError("invalid_param", "wci must be 0 or 1 (never aws).")
            return await self._start_dev_server(wci)
        target = spec.targets[variant]
        log = self.log_path(name)
        log.write_text("")
        env = self.settings.child_env(build_id)
        spawned = await self.runner.spawn(
            ["make", "-s", target], env=env, cwd=self.settings.root, log_path=log
        )
        rec = ProcRecord(
            name=name,
            pid=spawned.pid,
            pgid=spawned.pid,
            target=target,
            variant=variant,
            profile=self.settings.profile,
            started_at=iso(now()) or "",
        )
        self._save(rec)
        self._last.pop(name, None)
        self._waiters[name] = asyncio.create_task(self._wait(name, spawned))
        body = self.describe(name)
        self.bus.publish("process", body)
        return body

    async def _start_dev_server(self, wci: int) -> dict[str, Any]:
        log = self.log_path("dev-server")
        env = self.settings.child_env(extra={"WCI": str(wci)})
        result = await self.runner.run(
            ["make", "-s", "dev-server-up", f"WCI={wci}"],
            env=env,
            cwd=self.settings.root,
            within=60,
        )
        log.write_text(result.output)
        pid = self._dev_server_pid()
        if result.returncode != 0 or pid is None:
            raise ProcessError(
                "precondition_failed",
                "The dev server did not start.",
                f"See {log.relative_to(self.settings.root)} and .temporal/dev-server.log.",
            )
        rec = ProcRecord(
            name="dev-server",
            pid=pid,
            pgid=pid,
            target="dev-server-up",
            variant="default",
            profile=self.settings.profile,
            started_at=iso(now()) or "",
            wci=wci,
        )
        self._save(rec)
        self._last.pop("dev-server", None)
        body = self.describe("dev-server")
        self.bus.publish("process", body)
        return body

    async def _wait(self, name: str, spawned: Spawned) -> None:
        try:
            code = await spawned.wait()
        except asyncio.CancelledError:
            return
        expected = name in self._stopping
        self._waiters.pop(name, None)
        self._forget(name, exit_code=code)
        if code not in (0, None) and not expected:
            self.bus.alert(
                "error",
                f"{SPECS[name].label} exited with code {code}.",
                f"See its log: GET /api/processes/{name}/log",
            )

    def _forget(self, name: str, exit_code: int | None) -> None:
        spec = SPECS[name]
        # It is gone: until the next stack probe, its port and pollers are not evidence of
        # an external instance.
        self.ports_open -= set(spec.ports)
        if spec.polls:
            self.polled.discard(spec.polls)
        rec = self.records.pop(name, None)
        (self.proc_dir / f"{name}.json").unlink(missing_ok=True)
        expected = name in self._stopping
        self._stopping.discard(name)
        if rec is not None:
            ended = iso(now()) or ""
            self._last[name] = _Last(exit_code, ended, rec.variant, expected)
            # When each managed process ended (any cause, kill -9 from a terminal included):
            # a plain Support worker's lifetime ends here, not at its last history event.
            with contextlib.suppress(OSError), self.exits_path.open("a") as f:
                f.write(
                    json.dumps({"name": name, "at": ended, "started_at": rec.started_at}) + "\n"
                )
        self.bus.publish("process", self.describe(name))

    def _save(self, rec: ProcRecord) -> None:
        self.records[rec.name] = rec
        (self.proc_dir / f"{rec.name}.json").write_text(json.dumps(rec.to_json(), indent=2))

    async def stop(self, name: str, *, sig: str = "TERM") -> dict[str, Any]:
        spec = SPECS.get(name)
        if spec is None:
            raise ProcessError("not_found", f"No process {name!r}.")
        if sig not in ("TERM", "KILL"):
            raise ProcessError("invalid_param", "signal must be TERM or KILL.")
        rec = self.records.get(name)
        if name == "dev-server":
            if self._dev_server_pid() is None:
                raise ProcessError("precondition_failed", "The dev server is not running.")
            result = await self.runner.run(
                ["make", "-s", "dev-server-down"],
                env=self.settings.child_env(),
                cwd=self.settings.root,
                within=30,
            )
            with self.log_path(name).open("a") as log:
                log.write(result.output)
            self._stopping.add(name)
            self._forget(name, exit_code=0)
            return self.describe(name)
        if rec is None or not self._alive(rec):
            if self._external(spec):
                raise ProcessError(
                    "precondition_failed",
                    f"{spec.label} was started outside the console; stop it where it runs.",
                )
            raise ProcessError("precondition_failed", f"{spec.label} is not running.")
        self._stopping.add(name)
        if sig == "KILL":
            self.runner.signal_group(rec.pgid, signal.SIGKILL)
            with self.kills_path.open("a") as f:
                f.write(json.dumps({"name": name, "at": iso(now()), "pid": rec.pid}) + "\n")
        else:
            self.runner.signal_group(rec.pgid, signal.SIGTERM)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + STOP_GRACE_S
            while self.runner.group_alive(rec.pgid) and loop.time() < deadline:  # noqa: ASYNC110
                await asyncio.sleep(0.2)
            if self.runner.group_alive(rec.pgid):
                self.runner.signal_group(rec.pgid, signal.SIGKILL)
        waiter = self._waiters.get(name)
        if waiter is not None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(waiter), 5)
        if name in self.records:
            self._forget(name, exit_code=-9 if sig == "KILL" else -15)
        return self.describe(name)

    async def stop_all(self) -> list[str]:
        """Stop every managed process (the teardown recipe), dev server last."""
        stopped: list[str] = []
        for name in [n for n in self.records if n != "dev-server"]:
            with contextlib.suppress(ProcessError):
                await self.stop(name)
                stopped.append(name)
        return stopped

    def tail(self, name: str, lines: int = 200) -> list[str]:
        if name not in SPECS:
            raise ProcessError("not_found", f"No process {name!r}.")
        return tail_file(self.log_path(name), lines)


def tail_file(path: Path, lines: int = 200) -> list[str]:
    """The last ``lines`` lines of a file (no path ever comes from a request)."""
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 256 * 1024))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    return data.splitlines()[-lines:]
