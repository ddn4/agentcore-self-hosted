"""Opt-in AWS adapters (DESIGN.md 4.4, 4.5): CloudWatch sessions and the EKS failure demo.

Nothing here runs unless the console was started with ``CONSOLE_ALLOW_AWS=1``. The CloudWatch
reads reuse ``demo_testing.agentcore_logs`` (imported lazily; its boto3 clients are created
only inside its functions), run in a thread so the event loop never blocks. Tests pass fake
``sessions_for`` / ``wait_drained`` functions, so no boto3 client is ever created offline.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from demo_console.payloads import iso
from demo_console.settings import Settings


class AwsDisabled(PermissionError):
    """An AWS-backed action while ``CONSOLE_ALLOW_AWS`` is not 1."""


SessionsFor = Callable[[str, int, str | None], list[Any]]
WaitDrained = Callable[[str, int, float, str | None], list[Any]]


def _ms_iso(value: int | None) -> str | None:
    if value is None:
        return None
    return iso(datetime.fromtimestamp(value / 1000, UTC))


def session_summary(session: Any) -> dict[str, Any]:
    """A ``demo_testing.agentcore_logs.Session`` as JSON with full timestamps."""
    first = session.first
    return {
        "session": str(session.session_id)[:8],
        "app_start_iso": _ms_iso(first("app_start")),
        "invoked_iso": _ms_iso(first("invoked")),
        "connected_iso": _ms_iso(first("connected")),
        "idle_iso": _ms_iso(first("idle")),
        "released_iso": _ms_iso(first("released")),
        "lifetimes": session.count("invoked"),
        "drained": bool(session.drained),
    }


class CloudAdapter:
    def __init__(
        self,
        settings: Settings,
        *,
        sessions_for: SessionsFor | None = None,
        wait_drained: WaitDrained | None = None,
    ) -> None:
        self.settings = settings
        self._sessions_for = sessions_for
        self._wait_drained = wait_drained

    def require(self) -> None:
        if not self.settings.allow_aws:
            raise AwsDisabled(
                "This action calls AWS. Start the console with CONSOLE_ALLOW_AWS=1 to allow it."
            )

    def _impl(self) -> tuple[SessionsFor, WaitDrained]:
        sessions_for, wait_drained = self._sessions_for, self._wait_drained
        if sessions_for is None or wait_drained is None:
            from demo_testing import agentcore_logs

            sessions_for = sessions_for or agentcore_logs.sessions_for
            wait_drained = wait_drained or agentcore_logs.wait_drained
        return sessions_for, wait_drained

    async def sessions(
        self, workflow_id: str, since_ms: int, build: str | None
    ) -> list[dict[str, Any]]:
        self.require()
        sessions_for, _ = self._impl()
        found = await asyncio.to_thread(sessions_for, workflow_id, since_ms, build)
        return [session_summary(s) for s in found]

    async def wait_drained(
        self, workflow_id: str, since_ms: int, build: str | None, within: float = 300
    ) -> list[dict[str, Any]]:
        self.require()
        _, wait_drained = self._impl()
        found = await asyncio.to_thread(wait_drained, workflow_id, since_ms, within, build)
        return [session_summary(s) for s in found]


def failure_argv(workflow_id: str) -> list[str]:
    """``make failure`` (scripts/demo/failure.sh): kubectl scale on EKS, so AWS."""
    return ["make", "-s", "failure", f"WF={workflow_id}"]
