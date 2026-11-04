"""Which profile is behind 127.0.0.1:7233 (DESIGN.md 4.2): pidfiles plus a TLS probe.

- ``.temporal/dev-server.pid`` alive -> ``local``
- ``.temporal/kind-pf-*.pid`` alive -> ``kind`` or ``kind-plaintext``: a plaintext
  ``GetSystemInfo`` that fails the handshake means mTLS (``kind``)
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from temporalio.client import Client

PidAlive = Callable[[int], bool]
PlaintextProbe = Callable[[], Awaitable[bool | None]]


@dataclass(frozen=True)
class Detection:
    detected: str | None
    reason: str
    port_forward: dict[str, object] | None


def _pid(path: Path, alive: PidAlive) -> bool:
    try:
        pid = int(path.read_text().strip())
    except (OSError, ValueError):
        return False
    return alive(pid)


def pidfile_state(temporal_dir: Path, alive: PidAlive) -> dict[str, bool]:
    names = ("dev-server", "kind-pf-frontend", "kind-pf-web")
    return {name: _pid(temporal_dir / f"{name}.pid", alive) for name in names}


async def plaintext_probe(target: str = "127.0.0.1:7233", within: float = 3.0) -> bool | None:
    """``True``: a plaintext client got an answer; ``False``: refused (mTLS); ``None``: no
    listener (nothing to probe)."""
    host, _, port = target.partition(":")
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, int(port)), within)
        writer.close()
    except (OSError, TimeoutError):
        return None
    try:
        async with asyncio.timeout(within):
            await Client.connect(target, namespace="agents", tls=False)
    except Exception:
        return False
    return True


async def detect(
    temporal_dir: Path, alive: PidAlive, probe: PlaintextProbe = plaintext_probe
) -> Detection:
    state = pidfile_state(temporal_dir, alive)
    if state["dev-server"]:
        return Detection("local", "dev server pidfile", None)
    if state["kind-pf-frontend"] or state["kind-pf-web"]:
        pf = {"kind": "kind", "frontend": state["kind-pf-frontend"], "web": state["kind-pf-web"]}
        plaintext = await probe() if state["kind-pf-frontend"] else None
        if plaintext is True:
            return Detection("kind-plaintext", "kind port-forward, plaintext answers", pf)
        return Detection("kind", "kind port-forward" + (", mTLS" if plaintext is False else ""), pf)
    return Detection(None, "no dev server or port-forward pidfile", None)


MISMATCH_TEXT = {
    "local": "The dev server is running",
    "kind": "A kind port-forward is running",
    "kind-plaintext": "A kind port-forward (plaintext) is running",
}


def mismatch(configured: str, detected: str | None) -> str | None:
    if detected is None or detected == configured:
        return None
    return (
        f"{MISMATCH_TEXT[detected]}, but the console is on profile {configured}. "
        f"Switch to {detected}."
    )
