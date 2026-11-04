"""The local AgentCore API emulator (v0.7, no AWS).

``scripts/agentcore_emulator.py`` answers ``GetAgentRuntimeEndpoint`` READY and forwards
``InvokeAgentRuntime`` to the runtime's ``/invocations`` (the two calls the dev server's
WCI was observed to make), logging each one.
"""

from __future__ import annotations

import http.server
import importlib.util
import json
import threading
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, file: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / file)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


emulator = _load("agentcore_emulator", "agentcore_emulator.py")


# --- emulator ---------------------------------------------------------------------------


class _Runtime(http.server.BaseHTTPRequestHandler):
    seen: ClassVar[list[tuple[dict[str, Any], str]]] = []

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        _Runtime.seen.append((body, self.headers.get(emulator.SESSION_HEADER, "")))
        data = json.dumps({"message": "worker starting", "lifetime": len(_Runtime.seen)}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _serve(handler: type[http.server.BaseHTTPRequestHandler]) -> http.server.ThreadingHTTPServer:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def emulated(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    runtime = _serve(_Runtime)
    emulator.Emulator.runtime_url = f"http://127.0.0.1:{runtime.server_address[1]}"
    emulator.Emulator.log_path = tmp_path / "calls.jsonl"
    server = _serve(emulator.Emulator)
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", emulator.Emulator.log_path
    finally:
        server.shutdown()
        runtime.shutdown()


def test_get_runtime_endpoint_is_ready(emulated: tuple[str, Path]) -> None:
    url, log = emulated
    with urllib.request.urlopen(
        f"{url}/runtimes/support_supervisor_local/runtime-endpoints/b_abc/"
    ) as r:
        body = json.loads(r.read())
    assert body["status"] == "READY" and body["name"] == "b_abc"
    assert body["agentRuntimeEndpointArn"].endswith(
        "runtime/support_supervisor_local/runtime-endpoint/b_abc"
    )
    assert isinstance(body["createdAt"], str)  # the WCI's Go SDK rejects numeric timestamps
    assert json.loads(log.read_text())["op"] == "GetAgentRuntimeEndpoint"


def test_invoke_is_forwarded_to_the_runtime(emulated: tuple[str, Path]) -> None:
    url, log = emulated
    _Runtime.seen.clear()
    payload = {"deploymentName": "support-supervisor", "buildId": "abc1234"}
    req = urllib.request.Request(
        f"{url}/runtimes/support_supervisor_local/invocations?accountId=x&qualifier=b_abc1234",
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json", emulator.SESSION_HEADER: "s-1"},
        method="POST",
    )
    with urllib.request.urlopen(req) as r:
        assert json.loads(r.read())["message"] == "worker starting"
    assert _Runtime.seen == [(payload, "s-1")]
    entry = json.loads(log.read_text().splitlines()[-1])
    assert entry["op"] == "InvokeAgentRuntime" and entry["qualifier"] == "b_abc1234"
    assert entry["payload"] == payload and entry["session"] == "s-1" and entry["status"] == 200
