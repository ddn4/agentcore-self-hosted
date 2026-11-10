"""The kind stack's layers (DESIGN.md 2.3, 4.2): Docker VM, cluster, port-forward, client certs.

Each layer is one cheap command with a short timeout (``docker info``, ``kind get clusters``,
``kubectl get secret``) or a pidfile. ``KindProbe`` caches the answers for 15 s and refreshes
them in the background, so the stack snapshot never waits on a slow Docker. The same checks
are the skip checks of the "Bring up kind" recipe (actions.py), so the stack bar and the recipe
agree on the first missing layer.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import json
import time
from dataclasses import dataclass
from typing import Any, Final

from demo_console.detect import pidfile_state
from demo_console.runner import CommandRunner
from demo_console.settings import Settings

#: scripts/kind.sh ``CLUSTER``; kubectl/helm use the context ``kind-<cluster>``.
KIND_CLUSTER: Final[str] = "agentcore-demo"
KIND_CONTEXT: Final[str] = f"kind-{KIND_CLUSTER}"
#: Makefile ``DOCKER_CONTEXT``; colima names the context ``colima-<profile>``.
COLIMA_PROFILE: Final[str] = "agentcore"
DOCKER_CONTEXT: Final[str] = f"colima-{COLIMA_PROFILE}"
#: scripts/certs-pull.sh: one directory per client identity, plus ``.certs/ca.crt``.
CERT_IDENTITIES: Final[tuple[str, ...]] = ("operator", "agentcore-worker", "laptop-support")
FACTS_TTL_S: Final[float] = 15.0
CERTS_TTL_S: Final[float] = 60.0
CHECK_S: Final[float] = 4.0


def kind_env(settings: Settings, build_id: str | None = None) -> dict[str, str]:
    """``child_env`` plus what scripts/kind.sh pins: the Docker context and the kubeconfig."""
    return settings.child_env(
        build_id,
        {
            "DOCKER_CONTEXT": DOCKER_CONTEXT,
            "KUBECONFIG": str(settings.temporal_dir / f"kubeconfig-{KIND_CLUSTER}"),
        },
    )


async def _run(
    runner: CommandRunner, settings: Settings, argv: list[str], within: float = CHECK_S
) -> tuple[int, str]:
    try:
        res = await runner.run(argv, env=kind_env(settings), cwd=settings.root, within=within)
    except OSError as err:  # the binary is not installed
        return 127, str(err)
    return res.returncode, res.output


async def docker_up(runner: CommandRunner, settings: Settings) -> bool:
    code, _ = await _run(
        runner,
        settings,
        ["docker", "--context", DOCKER_CONTEXT, "info", "--format", "{{.ServerVersion}}"],
    )
    return code == 0


async def cluster_exists(runner: CommandRunner, settings: Settings) -> bool:
    code, out = await _run(runner, settings, ["kind", "get", "clusters"])
    return code == 0 and KIND_CLUSTER in out.split()


async def postgres_ready(runner: CommandRunner, settings: Settings) -> bool:
    """The last thing ``kind-up`` creates: a ready PostgreSQL Deployment in ``temporal``."""
    code, out = await _run(
        runner,
        settings,
        [
            "kubectl",
            "--context",
            KIND_CONTEXT,
            "get",
            "deploy/postgres",
            "-n",
            "temporal",
            "-o",
            "jsonpath={.status.readyReplicas}",
        ],
    )
    return code == 0 and out.strip() not in ("", "0")


async def helm_release(runner: CommandRunner, settings: Settings) -> tuple[str | None, bool | None]:
    """``(status, frontend_tls)`` of the ``temporal`` release; ``(None, None)`` without one."""
    base = ["helm", "--kube-context", KIND_CONTEXT]
    code, out = await _run(
        runner, settings, [*base, "status", "temporal", "-n", "temporal", "-o", "json"], 15
    )
    if code != 0:
        return None, None
    try:
        status = str(json.loads(out)["info"]["status"])
    except (ValueError, KeyError, TypeError):
        return None, None
    code, out = await _run(
        runner, settings, [*base, "get", "values", "temporal", "-n", "temporal", "-o", "json"], 15
    )
    tls: bool | None = None
    with contextlib.suppress(ValueError, KeyError, TypeError, AttributeError):
        values = json.loads(out) if code == 0 else {}
        tls = bool(
            ((values.get("server") or {}).get("tls") or {}).get("frontend", {}).get("enabled")
        )
    return status, tls


async def finance_image(runner: CommandRunner, settings: Settings) -> str | None:
    """The Finance Deployment's image (``…:kind-scripted`` or ``…:kind``), else ``None``."""
    code, out = await _run(
        runner,
        settings,
        [
            "kubectl",
            "--context",
            KIND_CONTEXT,
            "get",
            "deploy/agent-services-worker",
            "-n",
            "temporal-workers",
            "-o",
            "jsonpath={.spec.template.spec.containers[0].image}",
        ],
    )
    return out.strip() or None if code == 0 else None


async def cluster_ca(runner: CommandRunner, settings: Settings) -> bytes | None:
    """The root CA the cluster issues from (the operator Secret's ``ca.crt``)."""
    code, out = await _run(
        runner,
        settings,
        [
            "kubectl",
            "--context",
            KIND_CONTEXT,
            "get",
            "secret",
            "temporal-operator-tls",
            "-n",
            "temporal",
            "-o",
            r"jsonpath={.data.ca\.crt}",
        ],
    )
    if code != 0 or not out.strip():
        return None
    try:
        return base64.b64decode(out.strip())
    except ValueError:
        return None


def local_certs(settings: Settings) -> bool:
    certs = settings.root / ".certs"
    return (certs / "ca.crt").is_file() and all(
        (certs / name / "tls.crt").is_file() for name in CERT_IDENTITIES
    )


@dataclass(frozen=True)
class KindFacts:
    """``certs``: ``ok | missing | stale | unknown``; ``None`` on kind-plaintext (no mTLS)."""

    docker: bool
    cluster: bool | None
    port_forward: bool
    certs: str | None


def _fix(action: str, label: str, **params: Any) -> dict[str, Any]:
    return {"kind": "action", "action": action, "params": params, "label": label}


BRING_UP = ("start-recipe", "Bring up kind", {"recipe": "kind-up"})


def gap(facts: KindFacts | None, temporal_up: bool) -> dict[str, Any] | None:
    """The first missing layer, bottom up, as ``{layer, cause, fix}``; ``None`` when complete."""
    if facts is None:
        return None
    bring_up = _fix(BRING_UP[0], BRING_UP[1], **BRING_UP[2])
    if not facts.docker:
        cause = f"Docker VM {COLIMA_PROFILE} is stopped"
        return {"layer": "docker", "cause": cause, "fix": _fix("colima-up", "Start Docker VM")}
    if not facts.cluster:
        return {"layer": "cluster", "cause": "No kind cluster", "fix": bring_up}
    if not facts.port_forward:
        return {
            "layer": "port-forward",
            "cause": "Port-forward is off",
            "fix": _fix("pf", "Start port-forward"),
        }
    if not temporal_up:
        cause = "Port-forward is on, but Temporal on kind does not answer"
        return {"layer": "temporal", "cause": cause, "fix": bring_up}
    if facts.certs == "stale":
        cause = "Client certs are from an older cluster"
        return {"layer": "certs", "cause": cause, "fix": _fix("certs-pull", "Pull client certs")}
    if facts.certs == "missing":
        cause = "No client certs in .certs/"
        return {"layer": "certs", "cause": cause, "fix": _fix("certs-pull", "Pull client certs")}
    return None


class KindProbe:
    """Cached kind facts (15 s), refreshed in the background; ``None`` on profile local."""

    def __init__(self, settings: Settings, runner: CommandRunner) -> None:
        self.settings = settings
        self.runner = runner
        self._cached: tuple[float, str, KindFacts] | None = None
        self._certs: tuple[float, float, str] | None = None  # (when, ca.crt mtime, state)
        self._task: asyncio.Task[KindFacts] | None = None

    def invalidate(self) -> None:
        self._cached = None
        self._certs = None

    def last(self) -> KindFacts | None:
        """The cached facts for the current profile without a refresh (action descriptions)."""
        cached = self._cached
        if self.settings.profile == "local" or not cached or cached[1] != self.settings.profile:
            return None
        return cached[2]

    def port_forward(self) -> bool:
        state = pidfile_state(self.settings.temporal_dir, self.runner.pid_alive)
        return state["kind-pf-frontend"]

    async def facts(self, within: float = 6.0) -> KindFacts | None:
        profile = self.settings.profile
        if profile == "local":
            return None
        cached = self._cached if self._cached and self._cached[1] == profile else None
        if cached and time.monotonic() - cached[0] < FACTS_TTL_S:
            return dataclasses.replace(cached[2], port_forward=self.port_forward())
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._refresh(profile))
        if cached:  # stale while the refresh runs
            return dataclasses.replace(cached[2], port_forward=self.port_forward())
        try:
            return await asyncio.wait_for(asyncio.shield(self._task), within)
        except Exception:
            return None

    async def _refresh(self, profile: str) -> KindFacts:
        docker = await docker_up(self.runner, self.settings)
        cluster = await cluster_exists(self.runner, self.settings) if docker else None
        certs = await self.certs(cluster=bool(cluster)) if profile == "kind" else None
        found = KindFacts(docker, cluster, self.port_forward(), certs)
        if self.settings.profile == profile:
            self._cached = (time.monotonic(), profile, found)
        return found

    async def certs(self, *, cluster: bool) -> str:
        """``ok | missing | stale | unknown``: the local CA against the cluster's (cached 60 s)."""
        if not local_certs(self.settings):
            return "missing"
        if not cluster:
            return "unknown"
        ca = self.settings.root / ".certs" / "ca.crt"
        mtime = ca.stat().st_mtime
        hit = self._certs
        if hit and hit[1] == mtime and time.monotonic() - hit[0] < CERTS_TTL_S:
            return hit[2]
        remote = await cluster_ca(self.runner, self.settings)
        if remote is None:
            state = "unknown"
        else:
            state = "ok" if remote.strip() == ca.read_bytes().strip() else "stale"
        self._certs = (time.monotonic(), mtime, state)
        return state
