"""Screenshot the demo console with headless Chrome over the DevTools protocol (DESIGN.md 6.3).

    scripts/console-shot.sh <url-path> <out.png> [width] [height]     a page of the running console
    scripts/console-shot.sh --all                                       every fixture, no Temporal

A plain ``chrome --screenshot`` fires before the ES modules have rendered, so this drives
Chrome directly: set the viewport (a true 390 px phone width), load, wait, optionally click,
then capture. ``--all`` serves ``static/`` itself on a free 127.0.0.1 port and writes each
fixture at 1440x900 and 390x844, dark and light, into ``.temporal/console/shots/``. Chrome
runs with a throwaway profile and exits when done. Page errors are printed (exit 1 with
``--strict``).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import functools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections.abc import Iterator
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "services" / "demo-console" / "src" / "demo_console" / "static"
SHOTS = ROOT / ".temporal" / "console" / "shots"
CHROME = os.environ.get("CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
FIXTURES = (
    "case-a-completed",
    "case-b-input-required",
    "case-b-completed-serverless",
    "failure-retrying",
    "empty-stack",
)
SIZES = ((1440, 900), (390, 844))


class Page:
    """One CDP session on the first tab; console messages and exceptions are collected."""

    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self.n = 0
        self.errors: list[str] = []

    async def call(self, method: str, **params: Any) -> dict[str, Any]:
        self.n += 1
        my = self.n
        await self.ws.send(json.dumps({"id": my, "method": method, "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            event = msg.get("method")
            if event == "Runtime.exceptionThrown":
                details = msg["params"]["exceptionDetails"]
                text = details.get("exception", {}).get("description") or details.get("text")
                self.errors.append(f"exception: {text}"[:400])
            elif event == "Runtime.consoleAPICalled" and msg["params"]["type"] == "error":
                args = msg["params"]["args"]
                self.errors.append(
                    "console.error: "
                    + " ".join(str(a.get("value", a.get("description", ""))) for a in args)[:400]
                )
            if msg.get("id") == my:
                return msg.get("result", {})


@contextlib.contextmanager
def chrome() -> Iterator[str]:
    """A headless Chrome with a throwaway profile; yields the first page's websocket URL."""
    if not os.access(CHROME, os.X_OK):
        sys.exit(f"console-shot: no Chrome at {CHROME} (set CHROME=)")
    profile = tempfile.mkdtemp(prefix="console-shot-")
    port = 9400 + os.getpid() % 400
    proc = subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--hide-scrollbars",
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={port}",
            "--remote-allow-origins=http://127.0.0.1",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(150):
            with contextlib.suppress(OSError, StopIteration, ValueError):
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=1) as r:
                    tabs = json.load(r)
                yield next(t for t in tabs if t["type"] == "page")["webSocketDebuggerUrl"]
                return
            proc.poll()
            if proc.returncode is not None:
                break
            time.sleep(0.1)
        sys.exit("console-shot: Chrome did not open its DevTools port")
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


def save(out: Path, data: bytes) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)


async def shoot(
    ws_url: str,
    url: str,
    out: Path,
    width: int,
    height: int,
    *,
    wait: float,
    clicks: list[str],
    full: bool,
    reduced: bool,
) -> list[str]:
    async with connect(ws_url, max_size=2**27, origin=None) as ws:
        page = Page(ws)
        await page.call("Runtime.enable")
        await page.call("Page.enable")
        await page.call(
            "Emulation.setDeviceMetricsOverride",
            width=width,
            height=height,
            deviceScaleFactor=1,
            mobile=width < 600,
        )
        features = [{"name": "prefers-reduced-motion", "value": "reduce" if reduced else ""}]
        await page.call("Emulation.setEmulatedMedia", features=features)
        await page.call("Page.navigate", url=url)
        await asyncio.sleep(wait)
        for selector in clicks:
            expr = (
                f"(() => {{ const e = document.querySelector({json.dumps(selector)});"
                " if (!e) return 'missing'; e.click(); return 'ok'; })()"
            )
            res = await page.call("Runtime.evaluate", expression=expr, returnByValue=True)
            if res.get("result", {}).get("value") != "ok":
                page.errors.append(f"click: no element matches {selector}")
            await asyncio.sleep(1.5)
        params: dict[str, Any] = {"format": "png"}
        if full:
            metrics = await page.call("Page.getLayoutMetrics")
            h = min(int(metrics["cssContentSize"]["height"]), 8000)
            params["clip"] = {"x": 0, "y": 0, "width": width, "height": h, "scale": 1}
            params["captureBeyondViewport"] = True
        shot = await page.call("Page.captureScreenshot", **params)
        await asyncio.to_thread(save, out, base64.b64decode(shot["data"]))
        return page.errors


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass


@contextlib.contextmanager
def serve_static() -> Iterator[str]:
    handler = functools.partial(Quiet, directory=str(STATIC))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("path", nargs="?", help="URL path on the console, e.g. '/#/runs/support-1042'")
    ap.add_argument("out", nargs="?", type=Path)
    ap.add_argument("width", nargs="?", type=int, default=1440)
    ap.add_argument("height", nargs="?", type=int, default=900)
    ap.add_argument("--all", action="store_true", help="every fixture -> .temporal/console/shots/")
    ap.add_argument("--base", default=os.environ.get("CONSOLE_URL", "http://127.0.0.1:8099"))
    ap.add_argument("--wait", type=float, default=3.0, help="seconds to let the page render")
    ap.add_argument("--click", action="append", default=[], help="CSS selector to click first")
    ap.add_argument("--full", action="store_true", help="the whole page, not just the viewport")
    ap.add_argument("--reduced-motion", action="store_true")
    ap.add_argument("--strict", action="store_true", help="exit 1 on any page error")
    a = ap.parse_args(argv)

    jobs: list[tuple[str, Path, int, int, bool]] = []
    with contextlib.ExitStack() as stack:
        if a.all:
            base = stack.enter_context(serve_static())
            for fixture in FIXTURES:
                for w, h in SIZES:
                    for theme in ("dark", "light"):
                        url = f"{base}/index.html?fixture={fixture}&theme={theme}"
                        out = SHOTS / f"{fixture}-{w}-{theme}.png"
                        jobs.append((url, out, w, h, w < 600))
        else:
            if not a.path or not a.out:
                ap.error("give <url-path> <out.png>, or --all")
            url = a.path
            if not url.startswith("http"):
                url = a.base.rstrip("/") + "/" + url.lstrip("/")
            jobs.append((url, a.out, a.width, a.height, a.full))
        ws_url = stack.enter_context(chrome())
        failed = False
        for url, out, w, h, full in jobs:
            errors = asyncio.run(
                shoot(
                    ws_url,
                    url,
                    out,
                    w,
                    h,
                    wait=a.wait,
                    clicks=a.click,
                    full=full,
                    reduced=a.reduced_motion,
                )
            )
            print(f"console-shot: {url} -> {out}")
            for err in errors:
                failed = True
                print(f"  page error: {err}", file=sys.stderr)
    return 1 if failed and a.strict else 0


if __name__ == "__main__":
    sys.exit(main())
