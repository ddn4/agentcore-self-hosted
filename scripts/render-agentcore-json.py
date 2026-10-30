"""Render Support's AgentCore project files from templates (plan v0.7, runbook 5.4).

    uv run python scripts/render-agentcore-json.py --build-id 3f9c2a1 \\
        --out build/agentcore/agentcore

Writes ``agentcore.json`` (and ``aws-targets.json`` when ``AWS_ACCOUNT_ID`` is set) from
``services/support-agent/agentcore/*.tmpl``. Both outputs are gitignored: they carry the
Build ID and, from v0.8, account and network IDs.

Values come from the environment (runtime-injected values win, plan 3.2):

- PROJECT_NAME: ``AGENTCORE_PROJECT_NAME`` (default ``AgentcoreDemo``; CLI stack
  ``AgentCore-<project>-<target>``, runtime ``<project>_<runtime>``)
- RUNTIME_NAME: ``AGENTCORE_RUNTIME_NAME`` (default ``support_supervisor``)
- executionRoleArn: ``AGENTCORE_EXECUTION_ROLE_ARN`` (the ``30-agentcore-iam`` role; unset =
  the CLI creates one). Never committed: the rendered file is gitignored.
- IDLE_SESSION_TIMEOUT: ``AGENTCORE_IDLE_SESSION_TIMEOUT`` (default 900; CLI min 60)
- NETWORK_MODE: ``AGENTCORE_NETWORK_MODE`` (default VPC; PUBLIC is the v0.8 probe, ADR-0005)
- subnets / security groups: ``AGENTCORE_SUBNETS`` / ``AGENTCORE_SECURITY_GROUPS``
  (comma lists; placeholders until v0.12)
- ENDPOINT_NAME: always ``b_<build7>``, one endpoint per Build ID (ADR-0004)
- RUNTIME_VERSION: ``AGENTCORE_RUNTIME_VERSION`` (default 1)
- TEMPORAL_ADDRESS, TEMPORAL_TLS, TEMPORAL_TLS_SERVER_NAME: ``AGENTCORE_TEMPORAL_*``
  (placeholders until v0.12)
- TEMPORAL_MTLS_SECRET_ID (default ``temporal/agentcore-worker-mtls``)
- AGENTCORE_DEBOUNCE_SECONDS: ``AGENTCORE_CLOUD_DEBOUNCE_SECONDS`` (default 30, ADR-0006)
- AWS_REGION, BEDROCK_MODEL_ID, BEDROCK_EFFORT, SUPPORT_GIT_SHA (default ``git rev-parse HEAD``)

In PUBLIC mode ``networkConfig`` is dropped (AgentCore rejects it there; runbook 5.4).
``envVars`` with an empty value are dropped (v0.8 PUBLIC probe: ``TEMPORAL_MTLS_SECRET_ID=``
means no mTLS secret, so ``support_agent.mtls`` stays a no-op).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import string
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "services/support-agent/agentcore"
#: AgentCore endpoint names: no hyphens (runbook 5.4).
ENDPOINT_NAME_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_]{0,47}")


def endpoint_name(build_id: str) -> str:
    name = f"b_{build_id[:7]}"
    if not ENDPOINT_NAME_RE.fullmatch(name):
        raise SystemExit(f"endpoint name {name!r} does not match {ENDPOINT_NAME_RE.pattern}")
    return name


def _list(env: str, placeholder: str) -> str:
    items = [x.strip() for x in os.environ.get(env, "").split(",") if x.strip()]
    return json.dumps(items or [placeholder])


def values(build_id: str) -> dict[str, str]:
    env = os.environ.get
    git_sha = (
        env("SUPPORT_GIT_SHA")
        or subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False
        ).stdout.strip()
    )
    mode = env("AGENTCORE_NETWORK_MODE", "VPC").upper()
    if mode not in ("VPC", "PUBLIC"):
        raise SystemExit(f"AGENTCORE_NETWORK_MODE must be VPC or PUBLIC, got {mode!r}")
    idle = int(env("AGENTCORE_IDLE_SESSION_TIMEOUT", "900"))
    if not 60 <= idle <= 28800:
        raise SystemExit(f"AGENTCORE_IDLE_SESSION_TIMEOUT must be 60..28800, got {idle}")
    return {
        "PROJECT_NAME": env("AGENTCORE_PROJECT_NAME", "AgentcoreDemo"),
        "IDLE_SESSION_TIMEOUT": str(idle),
        "RUNTIME_NAME": env("AGENTCORE_RUNTIME_NAME", "support_supervisor"),
        "NETWORK_MODE": mode,
        "SUBNETS_JSON": _list("AGENTCORE_SUBNETS", "<private-subnet-a>"),
        "SECURITY_GROUPS_JSON": _list("AGENTCORE_SECURITY_GROUPS", "<agentcore-worker-sg>"),
        "ENDPOINT_NAME": endpoint_name(build_id),
        "RUNTIME_VERSION": str(int(env("AGENTCORE_RUNTIME_VERSION", "1"))),
        "BUILD_ID": build_id,
        "SUPPORT_GIT_SHA": git_sha,
        "TEMPORAL_ADDRESS": env("AGENTCORE_TEMPORAL_ADDRESS", "<temporal-frontend>:7233"),
        "TEMPORAL_TLS": env("AGENTCORE_TEMPORAL_TLS", "true"),
        "TEMPORAL_TLS_SERVER_NAME": env(
            "AGENTCORE_TEMPORAL_TLS_SERVER_NAME", "<temporal-frontend-dns-name>"
        ),
        "TEMPORAL_MTLS_SECRET_ID": env("TEMPORAL_MTLS_SECRET_ID", "temporal/agentcore-worker-mtls"),
        "AGENTCORE_DEBOUNCE_SECONDS": env("AGENTCORE_CLOUD_DEBOUNCE_SECONDS", "30"),
        "AWS_REGION": env("AWS_REGION", "us-east-1"),
        "BEDROCK_MODEL_ID": env("BEDROCK_MODEL_ID", "global.anthropic.claude-opus-5-5"),
        "BEDROCK_EFFORT": env("BEDROCK_EFFORT", "medium"),
        "AWS_ACCOUNT_ID": env("AWS_ACCOUNT_ID", ""),
    }


def render_agentcore_json(build_id: str) -> dict[str, object]:
    vals = values(build_id)
    text = string.Template((TEMPLATES / "agentcore.json.tmpl").read_text()).substitute(vals)
    doc = json.loads(text)
    runtime = doc["runtimes"][0]
    if vals["NETWORK_MODE"] == "PUBLIC":
        runtime.pop("networkConfig", None)
    runtime["envVars"] = [e for e in runtime["envVars"] if e["value"] != ""]
    role = os.environ.get("AGENTCORE_EXECUTION_ROLE_ARN", "").strip()
    if role:
        runtime["executionRoleArn"] = role
    return doc


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--out", type=Path, required=True, help="directory for agentcore.json")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    doc = render_agentcore_json(args.build_id)
    (args.out / "agentcore.json").write_text(json.dumps(doc, indent=2) + "\n")
    rt = doc["runtimes"][0]  # type: ignore[index]
    print(
        f"rendered {args.out / 'agentcore.json'}: runtime {rt['name']}, "
        f"endpoint {next(iter(rt['endpoints']))}, network {rt['networkMode']}"
    )
    vals = values(args.build_id)
    if vals["AWS_ACCOUNT_ID"]:
        targets = string.Template((TEMPLATES / "aws-targets.json.tmpl").read_text()).substitute(
            vals
        )
        (args.out / "aws-targets.json").write_text(targets)
        print(f"rendered {args.out / 'aws-targets.json'}")


if __name__ == "__main__":
    main()
