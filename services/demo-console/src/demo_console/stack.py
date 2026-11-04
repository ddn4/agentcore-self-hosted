"""The live stack as one snapshot (DESIGN.md 5.1 ``StackSnapshot``).

Temporal (system info, Nexus endpoint, Worker Deployment, pollers), the laptop ports
(AgentCore app ``/ping``, payments ``/healthz``, emulator TCP), pidfiles (profile detection),
the managed processes, and the resolved Build ID.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx

from demo_console.clients import Gateway, TemporalUnreachable
from demo_console.detect import Detection, detect, mismatch, plaintext_probe
from demo_console.lifetimes import app_state, parse_app_log, read_emulator
from demo_console.payloads import iso, now, parse_iso
from demo_console.procs import ProcessManager, tail_file
from demo_console.runner import CommandRunner
from demo_console.scenarios import StackFacts
from demo_console.settings import (
    AGENTS_NS,
    DEPLOYMENT_NAME,
    DEV_TQ,
    ENDPOINT,
    FINANCE_TQ,
    SERVICES_NS,
    SMOKE_TQ,
    VERSIONED_TQ,
    Settings,
)

POLLER_FRESH_S = 70.0
QUEUES = (
    (AGENTS_NS, DEV_TQ),
    (AGENTS_NS, VERSIONED_TQ),
    (SERVICES_NS, FINANCE_TQ),
    (AGENTS_NS, SMOKE_TQ),
)


class HttpProbe(Protocol):
    async def get(self, url: str, within: float = 2.0) -> tuple[int, Any] | None: ...

    async def post(
        self, url: str, body: Any, headers: dict[str, str], within: float = 10.0
    ) -> tuple[int, Any] | None: ...

    async def tcp_open(self, host: str, port: int, within: float = 1.0) -> bool: ...


class RealHttp:
    """httpx for HTTP, asyncio for TCP; 127.0.0.1 only."""

    async def get(self, url: str, within: float = 2.0) -> tuple[int, Any] | None:
        try:
            async with httpx.AsyncClient(timeout=within) as client:
                resp = await client.get(url)
        except httpx.HTTPError:
            return None
        return resp.status_code, _body(resp)

    async def post(
        self, url: str, body: Any, headers: dict[str, str], within: float = 10.0
    ) -> tuple[int, Any] | None:
        try:
            async with httpx.AsyncClient(timeout=within) as client:
                resp = await client.post(url, json=body, headers=headers)
        except httpx.HTTPError:
            return None
        return resp.status_code, _body(resp)

    async def tcp_open(self, host: str, port: int, within: float = 1.0) -> bool:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), within)
        except (OSError, TimeoutError):
            return False
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        return True


def _body(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return resp.text


def ping_status(body: Any) -> str | None:
    """``Healthy`` | ``HealthyBusy`` from the AgentCore app's ``/ping``."""
    if isinstance(body, dict):
        value = body.get("status")
        return str(value) if value else None
    if isinstance(body, str) and body.strip():
        return body.strip().strip('"')
    return None


def compute_kind(version: dict[str, Any] | None) -> str:
    """``none`` | ``local-wci`` (placeholder ARN) | ``unknown``.

    Read from the version's compute config providers.
    """
    if not version:
        return "none"
    providers = version.get("providers") or []
    if not providers:
        return "none"
    text = repr(providers)
    if "support_supervisor_local" in text or ("0" * 12) in text:
        return "local-wci"
    return "unknown"


_TICK = timedelta(microseconds=1)
_IDENTITY = re.compile(r"^(?P<pid>\d+)@(?P<host>[^/]+)")


def fresh(
    pollers: list[dict[str, Any]],
    window: float = POLLER_FRESH_S,
    *,
    host: str | None = None,
    pid_alive: Callable[[int], bool] | None = None,
) -> list[dict[str, Any]]:
    """Pollers seen in the last ``window`` seconds.

    The server lists a poller for about a minute after it stops; a poller of *this* laptop
    (``<pid>@<host>``) whose process is gone is dropped at once, so a stopped Worker shows as
    stopped (crash demo, "stop the Support worker").
    """
    cutoff = now() - timedelta(seconds=window)
    out = []
    for p in pollers:
        when = parse_iso(p.get("last_access"))
        if when is not None and when < cutoff:
            continue
        m = _IDENTITY.match(p.get("identity", ""))
        if (
            m
            and host
            and pid_alive
            and m.group("host") == host
            and not pid_alive(int(m.group("pid")))
        ):
            continue
        out.append(p)
    return out


_LIFETIME = re.compile(r"^(?P<pid>\d+)@(?P<host>[^/]+)/agentcore-lifetime-(?P<n>\d+)$")


def drop_drained_lifetimes(
    pollers: list[dict[str, Any]],
    ping: str | None,
    host: str | None,
    running: int | None = None,
) -> list[dict[str, Any]]:
    """Drop this laptop's AgentCore lifetimes that have drained.

    The app process outlives each Worker lifetime, so ``fresh`` keeps a drained lifetime's
    poller for the server's minute. ``/ping`` ``Healthy`` (not ``HealthyBusy``) means no
    lifetime is running; while one is (``running``, from the app log), every other lifetime
    of this app is gone. The stack bar then counts Support like the run view does.
    """
    if not host or ping not in ("Healthy", "HealthyBusy"):
        return pollers
    out = []
    for p in pollers:
        m = _LIFETIME.match(p.get("identity", ""))
        if m and m.group("host") == host:
            if ping == "Healthy":
                continue
            if running is not None and int(m.group("n")) != running:
                continue
        out.append(p)
    return out


@dataclass
class BuildId:
    value: str | None
    source: str | None


class StackProbe:
    def __init__(
        self,
        settings: Settings,
        gateway: Gateway,
        procs: ProcessManager,
        runner: CommandRunner,
        http: HttpProbe | None = None,
        *,
        detector: Callable[[], Awaitable[Detection]] | None = None,
    ) -> None:
        self.settings = settings
        self.gateway = gateway
        self.procs = procs
        self.runner = runner
        self.http = http or RealHttp()
        self._detector = detector
        self._detection: tuple[float, Detection] | None = None
        self._script_build: tuple[float, str | None] | None = None
        self.last: dict[str, Any] | None = None
        self.facts = StackFacts(profile=settings.profile)
        self.pollers: dict[str, list[dict[str, Any]]] = {}
        #: Worker identity -> the newest poller ``last_access`` the server reported (before
        #: ``fresh`` drops a stopped Worker): when a plain Worker last polled.
        self.poller_seen: dict[str, datetime] = {}
        #: This laptop's name as it appears in Worker identities (``<pid>@<host>``).
        self.hostname = socket.gethostname()

    # --- Build ID (DESIGN.md 4.4) ----------------------------------------------------------

    async def script_build_id(self) -> str | None:
        """``scripts/build-id.sh`` (refuses while uv.lock or Support is uncommitted)."""
        cached = self._script_build
        if cached and time.monotonic() - cached[0] < 60:
            return cached[1]
        value: str | None = None
        with contextlib.suppress(OSError):
            result = await self.runner.run(
                ["scripts/build-id.sh"],
                env=self.settings.child_env(),
                cwd=self.settings.root,
                within=30,
            )
            out = result.output.strip().splitlines()
            if result.returncode == 0 and out and out[-1].strip().isalnum():
                value = out[-1].strip()
        self._script_build = (time.monotonic(), value)
        return value

    async def resolve_build_id(self, current_build: str | None) -> BuildId:
        env = self.settings.base_env.get("TEMPORAL_BUILD_ID", "").strip()
        if env:
            return BuildId(env, "env")
        if current_build:
            return BuildId(current_build, "deployment")
        script = await self.script_build_id()
        if script:
            return BuildId(script, "script")
        return BuildId(None, None)

    async def build_id(self) -> str | None:
        """For callers outside a snapshot (procs, actions)."""
        if self.last is not None:
            return self.last["build_id"]["value"]
        current = None
        with contextlib.suppress(Exception):
            dep = await self.gateway.deployment(DEPLOYMENT_NAME)
            current = dep["current_build"] if dep else None
        return (await self.resolve_build_id(current)).value

    def dev_server_started(self) -> datetime | None:
        """When the running local dev server started (its pidfile's mtime), else ``None``."""
        if self.settings.profile != "local" or self.procs.dev_server_wci() is None:
            return None
        try:
            mtime = (self.settings.temporal_dir / "dev-server.pid").stat().st_mtime
        except OSError:
            return None
        return datetime.fromtimestamp(mtime, UTC)

    # --- detection -------------------------------------------------------------------------

    async def detection(self) -> Detection:
        cached = self._detection
        if cached and time.monotonic() - cached[0] < 15:
            return cached[1]
        if self._detector is not None:
            found = await self._detector()
        else:
            found = await detect(self.settings.temporal_dir, self.runner.pid_alive, plaintext_probe)
        self._detection = (time.monotonic(), found)
        return found

    # --- the snapshot ----------------------------------------------------------------------

    async def _temporal(self) -> dict[str, Any]:
        out: dict[str, Any] = {"up": False, "error": None}
        if not await self.gateway.ensure():
            out["error"] = self.gateway.last_error
            return out
        try:
            info = await self.gateway.system_info()
        except TemporalUnreachable as err:
            out["error"] = str(err)
            return out
        out["up"] = True
        out["server_version"] = info.get("server_version")

        async def safe(coro: Awaitable[Any]) -> Any:
            try:
                return await coro
            except Exception as err:  # one failed read must not hide the others
                return err

        endpoint, deployment, *pollers = await asyncio.gather(
            safe(self.gateway.nexus_endpoint(ENDPOINT)),
            safe(self.gateway.deployment(DEPLOYMENT_NAME)),
            *(safe(self.gateway.pollers(ns, tq)) for ns, tq in QUEUES),
        )
        out["endpoint"] = endpoint if isinstance(endpoint, dict) else None
        out["deployment"] = deployment if isinstance(deployment, dict) else None
        for p in pollers:
            for item in p if isinstance(p, list) else []:
                when = parse_iso(item.get("last_access"))
                ident = item.get("identity") or ""
                if when is not None and ident and when > self.poller_seen.get(ident, when - _TICK):
                    self.poller_seen[ident] = when
        out["pollers"] = {
            f"{ns}/{tq}": (
                fresh(p, host=self.hostname, pid_alive=self.runner.pid_alive)
                if isinstance(p, list)
                else []
            )
            for (ns, tq), p in zip(QUEUES, pollers, strict=True)
        }
        version = None
        current = (out["deployment"] or {}).get("current_build")
        if current:
            version = await safe(self.gateway.deployment_version(DEPLOYMENT_NAME, current))
        out["version"] = version if isinstance(version, dict) else None
        return out

    async def _laptop(self) -> dict[str, Any]:
        s = self.settings
        ports = (8233, 8080, 8081, s.emulator_port)
        opened = await asyncio.gather(*(self.http.tcp_open("127.0.0.1", p) for p in ports))
        out: dict[str, Any] = {"ports": {p for p, ok in zip(ports, opened, strict=True) if ok}}
        ping = await self.http.get(f"{s.agentcore_url}/ping") if 8080 in out["ports"] else None
        out["ping"] = ping_status(ping[1]) if ping and ping[0] == 200 else None
        payments: dict[str, Any] | None = None
        if s.profile == "local" and 8081 in out["ports"]:
            health = await self.http.get(f"{s.payments_url}/healthz")
            refunds = await self.http.get(f"{s.payments_url}/refunds")
            count = None
            if refunds and refunds[0] == 200:
                body = refunds[1]
                if isinstance(body, dict):
                    body = body.get("refunds", body)
                count = len(body) if isinstance(body, (list, dict)) else None
            payments = {"ok": bool(health and health[0] == 200), "refunds": count}
        out["payments"] = payments
        return out

    async def snapshot(self) -> dict[str, Any]:
        s = self.settings
        temporal, laptop, detection = await asyncio.gather(
            self._temporal(), self._laptop(), self.detection()
        )
        ports: set[int] = laptop["ports"]
        pollers: dict[str, list[dict[str, Any]]] = temporal.get("pollers") or {
            f"{ns}/{tq}": [] for ns, tq in QUEUES
        }
        app_events = parse_app_log(tail_file(self.procs.log_path("agentcore-local"), 4000))
        app = app_state(app_events)
        key = f"{AGENTS_NS}/{VERSIONED_TQ}"
        pollers[key] = drop_drained_lifetimes(
            pollers.get(key, []),
            laptop["ping"],
            self.hostname,
            running=app["lifetime"] if app["running"] else None,
        )
        self.pollers = pollers
        polled = {key for key, items in pollers.items() if items}
        self.procs.ports_open = ports
        self.procs.polled = polled

        deployment = temporal.get("deployment")
        current = deployment.get("current_build") if deployment else None
        build = await self.resolve_build_id(current)
        self.procs.build_id_hint = build.value
        self.procs.build_id_known = True
        compute = compute_kind(temporal.get("version")) if current else "none"

        wci_file = self.procs.dev_server_wci()
        if s.profile == "local":
            wci_mode = {"1": "emulator", "local": "emulator"}.get(
                wci_file or "", "off" if wci_file is not None else None
            )
        else:
            wci_mode = "cluster"
        # Scoped to this dev server: after dev-server-reset the old JSONL describes a database
        # that no longer exists.
        dev_since = self.dev_server_started()
        emulator_calls = (
            [
                c
                for c in read_emulator(s.temporal_dir / "agentcore-emulator.jsonl")
                if dev_since is None or (c["at"] is not None and c["at"] >= dev_since)
            ]
            if s.profile == "local"
            else []
        )

        n_dev = len(pollers.get(f"{AGENTS_NS}/{DEV_TQ}", []))
        n_ver = len(pollers.get(f"{AGENTS_NS}/{VERSIONED_TQ}", []))
        n_fin = len(pollers.get(f"{SERVICES_NS}/{FINANCE_TQ}", []))
        n_smoke = len(pollers.get(f"{AGENTS_NS}/{SMOKE_TQ}", []))
        agentcore_up = 8080 in ports and laptop["ping"] is not None
        emulator_up = s.emulator_port in ports

        if wci_mode == "emulator" and compute == "local-wci" and (emulator_up or agentcore_up):
            shape = "serverless-wci"
        elif agentcore_up:
            shape = "serverless-invoke"
        elif n_dev or self.procs.running("worker-support"):
            shape = "plain"
        else:
            shape = "unknown"

        variants = [
            v
            for v in (
                self.procs.managed_variant(name)
                for name in ("worker-finance", "worker-support", "agentcore-local")
            )
            if v is not None
        ]
        if not variants:
            model = {"mode": "unknown", "source": "unknown"}
        elif all(v == "scripted" for v in variants):
            model = {"mode": "scripted", "source": "managed"}
        elif all(v == "live" for v in variants):
            model = {"mode": "live", "source": "managed"}
        else:
            model = {"mode": "mixed", "source": "managed"}

        components = self._components(
            temporal, laptop, n_fin, n_dev, n_ver, shape, agentcore_up, emulator_up, app
        )
        configured = s.profile
        snap: dict[str, Any] = {
            "checked_at": iso(now()),
            "profile": {
                "configured": configured,
                "detected": detection.detected,
                "mismatch": mismatch(configured, detection.detected),
                "tls": s.tls,
            },
            "shape": shape,
            "model": model,
            "build_id": {"value": build.value, "source": build.source},
            "deployment": {
                "name": DEPLOYMENT_NAME,
                "current_build": current,
                "compute": compute if deployment else None,
                "behavior": "PINNED" if deployment else None,
            },
            "wci": {
                "mode": wci_mode,
                "invokes_total": len(emulator_calls) if s.profile == "local" else None,
            },
            "agentcore_app": {
                "status": laptop["ping"] or "down",
                # The app numbers its own lifetimes; when it is down its old log says nothing.
                "lifetime": app["lifetime"] if laptop["ping"] else None,
                "url": s.agentcore_url,
            },
            "components": components,
            "pollers": pollers,
            "port_forward": detection.port_forward if configured != "local" else None,
        }
        if temporal.get("error") and not temporal["up"]:
            snap["temporal_error"] = temporal["error"]
        self.facts = StackFacts(
            profile=configured,
            temporal_up=temporal["up"],
            endpoint_ok=bool(temporal.get("endpoint")),
            finance_pollers=n_fin,
            support_dev_pollers=n_dev,
            smoke_pollers=n_smoke,
            payments_up=bool(laptop["payments"] and laptop["payments"]["ok"]),
            agentcore_up=agentcore_up,
            emulator_up=emulator_up,
            dev_server_wci=wci_file,
            build_id=build.value,
            current_build=current,
            compute=compute,
            managed=dict.fromkeys(self.procs.records, True),
            shape=shape,
        )
        self.last = snap
        return snap

    def _components(
        self,
        temporal: dict[str, Any],
        laptop: dict[str, Any],
        n_fin: int,
        n_dev: int,
        n_ver: int,
        shape: str,
        agentcore_up: bool,
        emulator_up: bool,
        app: dict[str, Any],
    ) -> list[dict[str, Any]]:
        s = self.settings
        local = s.profile == "local"
        ports: set[int] = laptop["ports"]

        def proc_status(up: bool, name: str) -> str:
            if not up:
                return "down"
            return "up" if name in self.procs.records else "external"

        out: list[dict[str, Any]] = []
        if temporal["up"]:
            detail = f"127.0.0.1:7233, server {temporal.get('server_version') or '?'}"
            status = proc_status(True, "dev-server") if local else "up"
        else:
            # The page adds the fix (a Start button locally, the pf command elsewhere).
            detail = "Temporal is not reachable at 127.0.0.1:7233"
            status = "down"
        comp: dict[str, Any] = {
            "id": "temporal",
            "label": "Temporal",
            "status": status,
            "detail": detail,
            "group": "temporal",
        }
        if local:
            comp["process"] = "dev-server"
        out.append(comp)
        out.append(
            {
                "id": "web-ui",
                "label": "Web UI",
                "status": "up" if 8233 in ports else "down",
                "detail": s.ui_base,
                "group": "temporal",
            }
        )
        ep = temporal.get("endpoint")
        out.append(
            {
                "id": "endpoint",
                "label": "Nexus endpoint",
                "status": "up" if ep else ("down" if temporal["up"] else "unknown"),
                "detail": (
                    f"finance-agent → {ep['namespace']}/{ep['task_queue']}"
                    if ep
                    else "finance-agent is missing (make nexus-endpoint)"
                ),
                "group": "temporal",
            }
        )
        fin: dict[str, Any] = {
            "id": "finance",
            "label": "Finance workers",
            "status": "up" if n_fin else ("down" if temporal["up"] else "unknown"),
            "detail": f"{n_fin} poller{'s' if n_fin != 1 else ''}",
            "count": n_fin,
            "group": "finance",
        }
        if local:
            fin["process"] = "worker-finance"
            if n_fin and "worker-finance" not in self.procs.records:
                fin["status"] = "external"
        out.append(fin)
        serverless = shape in ("serverless-invoke", "serverless-wci")
        n_support = n_ver if serverless else n_dev
        queue = VERSIONED_TQ if serverless else DEV_TQ
        if n_support:
            sup_status = "up"
        elif serverless:
            sup_status = "zero"
        else:
            sup_status = "down" if temporal["up"] else "unknown"
        sup: dict[str, Any] = {
            "id": "support",
            "label": "Support workers",
            "status": sup_status,
            "detail": f"{n_support} on {queue}",
            "count": n_support,
            "group": "support",
        }
        if not serverless:
            sup["process"] = "worker-support"
        out.append(sup)
        ping = laptop["ping"]
        if ping == "HealthyBusy":
            detail = f"HealthyBusy (lifetime {app['lifetime'] or '?'} running)"
        elif ping:
            detail = f"{ping} (idle)"
        else:
            detail = "not running on :8080"
        out.append(
            {
                "id": "agentcore",
                "label": "AgentCore app",
                "status": proc_status(agentcore_up, "agentcore-local"),
                "detail": detail,
                "group": "support",
                "process": "agentcore-local",
            }
        )
        if local:
            out.append(
                {
                    "id": "emulator",
                    "label": "WCI emulator",
                    "status": proc_status(emulator_up, "agentcore-emulator"),
                    "detail": f":{s.emulator_port}",
                    "group": "support",
                    "process": "agentcore-emulator",
                }
            )
            pay = laptop["payments"]
            refunds = pay.get("refunds") if pay else None
            out.append(
                {
                    "id": "payments",
                    "label": "Payments",
                    "status": proc_status(bool(pay and pay["ok"]), "payments-mock"),
                    "detail": (
                        f"{refunds} refund{'s' if refunds != 1 else ''}"
                        if refunds is not None
                        else ":8081"
                    ),
                    "group": "finance",
                    "process": "payments-mock",
                }
            )
        return out
