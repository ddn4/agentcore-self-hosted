"""``create_app``: the Starlette app and the services behind it (DESIGN.md 4, 7)."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from demo_console.actions import ActionService
from demo_console.clients import Clients, Gateway
from demo_console.cloud import CloudAdapter
from demo_console.events import EventBus
from demo_console.jobs import JobStore
from demo_console.lifetimes import WciReader
from demo_console.payloads import parse_iso
from demo_console.procs import ProcessManager
from demo_console.routes import ROUTES, Guard
from demo_console.runner import AsyncioRunner, CommandRunner
from demo_console.runs import RunService
from demo_console.scenarios import Scenario, load_catalog
from demo_console.settings import Settings
from demo_console.stack import HttpProbe, RealHttp, StackProbe
from demo_console.stream import Intervals, StreamHub

STATIC_DIR = Path(__file__).parent / "static"

PLACEHOLDER = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Demo console</title>
<style>
:root { --page: #E8ECEF; --ink: #14202B; --muted: #5A6675; }
@media (prefers-color-scheme: dark) { :root { --page: #141A21; --ink: #E4EAF0; --muted: #95A1B0; } }
body { background: var(--page); color: var(--ink); margin: 0; padding: 32px 16px;
       font: 15px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 68ch; margin: 0 auto; }
code { font-family: ui-monospace, "SF Mono", Menlo, monospace; }
p { color: var(--muted); }
</style></head>
<body><main>
<h1>Demo console</h1>
<p>The backend is running, but the page itself (<code>static/index.html</code>) is not
there yet. The API answers under <code>/api/</code>: try <a href="/api/stack">/api/stack</a>,
<a href="/api/scenarios">/api/scenarios</a> or <a href="/api/runs">/api/runs</a>.</p>
</main></body></html>
"""


class Console:
    """Every service of one console process (``app.state.console``)."""

    def __init__(
        self,
        settings: Settings,
        gateway: Gateway,
        runner: CommandRunner,
        http: HttpProbe,
        cloud: CloudAdapter,
        catalog: dict[str, Scenario],
        intervals: Intervals | None = None,
        settle_s: float = 10.0,
    ) -> None:
        self.settings = settings
        self.gateway = gateway
        self.runner = runner
        self.http = http
        self.cloud = cloud
        self.catalog = catalog
        self.settle_s = settle_s
        self.bus = EventBus()
        self.procs = ProcessManager(settings, runner, self.bus)
        self.jobs = JobStore(settings, runner, self.bus)
        self.stack = StackProbe(settings, gateway, self.procs, runner, http)
        self.procs.build_id_provider = self.stack.build_id
        self.runs = RunService(
            settings, gateway, self.procs, self.stack, self.jobs, catalog, WciReader(gateway)
        )
        self.actions = ActionService(
            settings,
            self.procs,
            self.jobs,
            self.stack,
            http,
            cloud,
            on_cloudwatch=lambda wf, result: self.runs.save_record(wf, cloudwatch=result),
            run_started_at=self._run_started_at,
        )
        self.hub = StreamHub(settings, self.stack, self.runs, self.bus, intervals)
        self.runs.stack_refresh = self.hub.stack_cache.get

    async def _run_started_at(self, workflow_id: str) -> float | None:
        with contextlib.suppress(Exception):
            view = await self.runs.view(workflow_id)
            when = parse_iso(view.get("started_at"))
            return when.timestamp() if when else None
        return None

    async def startup(self) -> None:
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        adopted = self.procs.adopt()
        self.procs.start_monitor()
        self.jobs.adopt()
        if adopted:
            self.bus.alert("info", f"Adopted running processes: {', '.join(adopted)}.")

    async def shutdown(self) -> None:
        await self.procs.stop_monitor()
        await self.jobs.shutdown()
        await self.gateway.close()

    async def switch_profile(self, profile: str) -> None:
        """Reconnect with another profile (the caller checked the managed processes)."""
        self.settings.profile = profile
        await self.gateway.close()
        self.stack._detection = None
        self.stack.last = None
        self.stack.facts.profile = profile
        self.hub.stack_cache.invalidate()
        self.hub.runs_cache.invalidate()


def create_app(
    settings: Settings | None = None,
    *,
    clients: Gateway | None = None,
    runner: CommandRunner | None = None,
    http: HttpProbe | None = None,
    cloud: CloudAdapter | None = None,
    intervals: Intervals | None = None,
    static_dir: Path | None = None,
    settle_s: float = 10.0,
) -> Starlette:
    settings = settings or Settings.from_env()
    gateway: Gateway = clients if clients is not None else Clients(settings)
    c = Console(
        settings,
        gateway,
        runner or AsyncioRunner(),
        http or RealHttp(),
        cloud or CloudAdapter(settings),
        load_catalog(),
        intervals,
        settle_s,
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        await c.startup()
        try:
            yield
        finally:
            await c.shutdown()

    static = static_dir or STATIC_DIR
    routes: list[Any] = list(ROUTES)
    if (static / "index.html").is_file():
        routes.append(Mount("/", app=StaticFiles(directory=static, html=True), name="static"))
    else:

        async def placeholder(request: Request) -> Response:
            return HTMLResponse(PLACEHOLDER)

        routes.append(Route("/", placeholder))

    app = Starlette(
        routes=routes,
        middleware=[Middleware(Guard, port=settings.port)],
        lifespan=lifespan,
    )
    app.state.console = c
    return app
