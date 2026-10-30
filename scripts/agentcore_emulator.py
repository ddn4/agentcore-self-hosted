"""Local stand-in for the two AgentCore APIs the Temporal WCI calls (plan v0.7, no AWS).

    uv run python scripts/agentcore_emulator.py            (make agentcore-emulator)

Finding (v0.7, ledger 23): the dev server (CLI 1.9.1 / server 1.32.0) **contains the
Worker Controller Instance** and its AWS SDK honours ``AWS_ENDPOINT_URL``, the same
mechanism Temporal documents for testing Lambda Serverless Workers with LocalStack.
``make dev-server WCI=1`` starts the dev server with the WCI enabled, dummy credentials
and ``AWS_ENDPOINT_URL`` pointing here, so nothing leaves the laptop. This emulator
answers the two calls the WCI makes (observed on the wire, not guessed):

- ``GET /runtimes/{runtimeId}/runtime-endpoints/{endpointName}/``
  (control plane ``GetAgentRuntimeEndpoint``): answers ``READY``;
- ``POST /runtimes/{runtimeId}/invocations?accountId=..&qualifier={endpointName}``
  (data plane ``InvokeAgentRuntime``, header
  ``X-Amzn-Bedrock-AgentCore-Runtime-Session-Id``, body ``{deploymentName, buildId}``):
  forwarded to the local runtime's ``/invocations`` (``make agentcore-local``, :8080),
  whose answer is returned.

Every call is appended as one JSON line to ``.temporal/agentcore-emulator.jsonl`` (time,
operation, session ID, payload, the runtime's answer): the local equivalent of the
AgentCore session count that v0.8+ reads from CloudWatch, and the evidence for "who
invoked the Worker, when" (Q15, local and informational only).

What it does *not* emulate: STS AssumeRole and the External ID check (the dev server
runs with ``workercontroller.compute_providers.aws.require_role_and_external_id=false``
and versions use ``--aws-agentcore-skip-role-and-external-id`` locally), sessions,
microVMs, ``/ping`` polling, idle timeouts. Those are v0.8/v0.12 (real AgentCore).
Stdlib only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import http.server
import json
import os
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

ENDPOINT_PATH = re.compile(r"^/runtimes/([^/]+)/runtime-endpoints/([^/]+)/?$")
INVOKE_PATH = re.compile(r"^/runtimes/([^/]+)/invocations$")
SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"
_lock = threading.Lock()


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Emulator(http.server.BaseHTTPRequestHandler):
    runtime_url = "http://127.0.0.1:8080"
    log_path = Path(".temporal/agentcore-emulator.jsonl")
    region = "us-east-1"

    def log_message(self, format: str, *args: Any) -> None:  # quiet the default access log
        pass

    def _record(self, entry: dict[str, Any]) -> None:
        entry = {"time": _now(), **entry}
        line = json.dumps(entry, sort_keys=True)
        with _lock:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a") as f:
                f.write(line + "\n")
        print(line, flush=True)

    def _reply(self, code: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        m = ENDPOINT_PATH.match(path)
        if not m:
            self._record({"op": "unknown", "method": "GET", "path": path})
            self._reply(404, {"message": f"emulator: no route for GET {path}"})
            return
        runtime_id, endpoint = unquote(m.group(1)), unquote(m.group(2))
        account = "0" * 12  # placeholder account: never a real one
        runtime_arn = f"arn:aws:bedrock-agentcore:{self.region}:{account}:runtime/{runtime_id}"
        self._record({"op": "GetAgentRuntimeEndpoint", "runtime": runtime_id, "endpoint": endpoint})
        stamp = "2026-01-01T00:00:00Z"  # restJson timestamps must be strings for the Go SDK
        self._reply(
            200,
            {
                "agentRuntimeArn": runtime_arn,
                "agentRuntimeEndpointArn": f"{runtime_arn}/runtime-endpoint/{endpoint}",
                "id": endpoint,
                "name": endpoint,
                "status": "READY",
                "liveVersion": "1",
                "targetVersion": "1",
                "createdAt": stamp,
                "lastUpdatedAt": stamp,
            },
        )

    def do_POST(self) -> None:
        parts = urlsplit(self.path)
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        m = INVOKE_PATH.match(parts.path)
        if not m:
            self._record({"op": "unknown", "method": "POST", "path": parts.path})
            self._reply(404, {"message": f"emulator: no route for POST {parts.path}"})
            return
        qualifier = parse_qs(parts.query).get("qualifier", [""])[0]
        session = self.headers.get(SESSION_HEADER, "")
        try:
            payload: Any = json.loads(body or b"{}")
        except json.JSONDecodeError:
            payload = body.decode(errors="replace")
        request = urllib.request.Request(
            f"{self.runtime_url}/invocations",
            data=body or b"{}",
            headers={"content-type": "application/json", SESSION_HEADER: session},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:
                answer = json.loads(resp.read() or b"{}")
                code = resp.status
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as err:
            # Like an AgentCore runtime that fails to start: the WCI sees an error.
            answer, code = {"message": f"emulator: runtime unreachable: {err}"}, 502
        self._record(
            {
                "op": "InvokeAgentRuntime",
                "runtime": unquote(m.group(1)),
                "qualifier": qualifier,
                "session": session,
                "payload": payload,
                "status": code,
                "answer": answer,
            }
        )
        self._reply(code, answer)


def main() -> None:
    parser = argparse.ArgumentParser(description="AgentCore API emulator for the local WCI")
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("AGENTCORE_EMULATOR_PORT", "4599"))
    )
    parser.add_argument(
        "--runtime-url", default=os.environ.get("AGENTCORE_URL", Emulator.runtime_url)
    )
    parser.add_argument("--log", type=Path, default=Emulator.log_path)
    args = parser.parse_args()
    Emulator.runtime_url = args.runtime_url.rstrip("/")
    Emulator.log_path = args.log
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Emulator)
    print(
        f"agentcore-emulator: http://127.0.0.1:{args.port} -> runtime {Emulator.runtime_url}; "
        f"log {args.log}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
