"""AgentCore project files and the local AgentCore API emulator (v0.7, no AWS).

- ``scripts/render-agentcore-json.py``: endpoint ``b_<build7>`` (no hyphens), the Build ID
  in the runtime env, PUBLIC drops ``networkConfig``, no account IDs in the output.
- ``scripts/agentcore_emulator.py``: answers ``GetAgentRuntimeEndpoint`` READY and forwards
  ``InvokeAgentRuntime`` to the runtime's ``/invocations`` (the two calls the dev server's
  WCI was observed to make), logging each one.
"""

from __future__ import annotations

import http.server
import importlib.util
import json
import re
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


render = _load("render_agentcore_json", "render-agentcore-json.py")
emulator = _load("agentcore_emulator", "agentcore_emulator.py")


def _env(doc: dict[str, Any]) -> dict[str, str]:
    return {e["name"]: e["value"] for e in doc["runtimes"][0]["envVars"]}


def test_vpc_render(monkeypatch: pytest.MonkeyPatch) -> None:
    # Hermetic: a shell that sourced .generated/foundation.env (v0.10+) exports the real
    # subnets / SG / execution role ARN, which this test must not see (found at v0.11).
    for var in (
        "AGENTCORE_NETWORK_MODE",
        "AGENTCORE_EXECUTION_ROLE_ARN",
        "AGENTCORE_SUBNETS",
        "AGENTCORE_SECURITY_GROUPS",
        "AGENTCORE_TEMPORAL_ADDRESS",
        "AGENTCORE_TEMPORAL_TLS_SERVER_NAME",
        "AGENTCORE_IDLE_SESSION_TIMEOUT",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SUPPORT_GIT_SHA", "deadbeef")
    doc = render.render_agentcore_json("3f9c2a1")
    rt = doc["runtimes"][0]
    assert doc["version"] == 2, "AgentCore CLI 1.0.x project format (version 1 = v0 compat mode)"
    assert rt["name"] == "support_supervisor"
    assert rt["build"] == "CodeZip" and rt["runtimeVersion"] == "PYTHON_3_12"
    assert (
        rt["entrypoint"] == "agentcore_worker.py" and rt["codeLocation"] == "app/support_supervisor"
    )
    assert rt["authorizerType"] == "AWS_IAM" and rt["networkMode"] == "VPC"
    assert set(rt["networkConfig"]) == {"subnets", "securityGroups"}
    assert list(rt["endpoints"]) == ["b_3f9c2a1"]
    env = _env(doc)
    assert env["TEMPORAL_BUILD_ID"] == "3f9c2a1"
    assert env["TEMPORAL_DEPLOYMENT_NAME"] == "support-supervisor"
    assert env["TEMPORAL_TASK_QUEUE"] == "support-tq"
    assert env["TEMPORAL_NAMESPACE"] == "agents"
    assert env["SUPPORT_GIT_SHA"] == "deadbeef"
    assert env["AGENTCORE_DEBOUNCE_SECONDS"] == "30"  # cloud demo (ADR-0006)
    assert env["BEDROCK_MODEL_ID"] == "global.anthropic.claude-opus-5-5"
    text = json.dumps(doc)
    assert not re.search(r"[0-9]{12}", text), "no account IDs in agentcore.json"
    assert "TLS_CLIENT_KEY" not in text, "cert material never goes in agentcore.json"


def test_public_render_drops_network_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTCORE_NETWORK_MODE", "PUBLIC")
    rt = render.render_agentcore_json("3f9c2a1")["runtimes"][0]
    assert rt["networkMode"] == "PUBLIC" and "networkConfig" not in rt


def test_v08_probe_render(monkeypatch: pytest.MonkeyPatch) -> None:
    """v0.8 PUBLIC probe: no mTLS secret, existing execution role, short idle timeout."""
    monkeypatch.setenv("AGENTCORE_NETWORK_MODE", "PUBLIC")
    monkeypatch.setenv("TEMPORAL_MTLS_SECRET_ID", "")
    monkeypatch.setenv("AGENTCORE_TEMPORAL_ADDRESS", "127.0.0.1:7233")
    monkeypatch.setenv("AGENTCORE_IDLE_SESSION_TIMEOUT", "120")
    monkeypatch.setenv("AGENTCORE_EXECUTION_ROLE_ARN", "arn:aws:iam::<acct>:role/x")
    doc = render.render_agentcore_json("3f9c2a1")
    rt = doc["runtimes"][0]
    assert doc["name"] == "AgentcoreDemo"
    assert doc["tags"]["Project"] == "agentcore-self-hosted"
    assert rt["executionRoleArn"].endswith(":role/x")
    assert rt["lifecycleConfiguration"]["idleRuntimeSessionTimeout"] == 120
    env = _env(doc)
    assert "TEMPORAL_MTLS_SECRET_ID" not in env, "empty values are dropped"
    assert env["TEMPORAL_ADDRESS"] == "127.0.0.1:7233"
    assert env["TEMPORAL_CONNECT_TIMEOUT_SECONDS"] == "10"


def test_idle_timeout_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTCORE_IDLE_SESSION_TIMEOUT", "30")
    with pytest.raises(SystemExit):
        render.render_agentcore_json("3f9c2a1")


@pytest.mark.parametrize("build_id", ["3f9c2a1", "0abcdef", "devtest"])
def test_endpoint_name_matches_the_agentcore_rule(build_id: str) -> None:
    name = render.endpoint_name(build_id)
    assert re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,47}", name) and "-" not in name


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
