"""Prove that Claude Opus 5.5 answers on Bedrock with no inference parameters.

Calls the Converse API (what Strands' BedrockModel uses) once per inference
profile and checks the request that actually went on the wire: no
inferenceConfig (temperature/top_p/top_k), no thinking config and no toolConfig
(so no forced tool_choice). Opus 5.5 rejects all of those with a 400.

    uv run python scripts/bedrock_ping.py                    # global. and us. profiles
    uv run python scripts/bedrock_ping.py --profile global   # just one

Read-only: it invokes a model and creates no AWS resources. Costs a few tokens.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import boto3
from botocore.config import Config

MODEL = "anthropic.claude-opus-5-5"
FORBIDDEN_TOP_LEVEL = ("inferenceConfig", "toolConfig")
FORBIDDEN_ANYWHERE = ("temperature", "top_p", "topP", "top_k", "topK", "thinking")


def check_wire(body: dict[str, Any]) -> list[str]:
    problems = [f"top-level '{k}' present" for k in FORBIDDEN_TOP_LEVEL if k in body]
    flat = json.dumps(body.get("additionalModelRequestFields", {}))
    problems += [f"'{k}' in additionalModelRequestFields" for k in FORBIDDEN_ANYWHERE if k in flat]
    return problems


def ping(profile_prefix: str, region: str) -> bool:
    model_id = f"{profile_prefix}.{MODEL}"
    client = boto3.client(
        "bedrock-runtime", region_name=region, config=Config(retries={"max_attempts": 0})
    )
    wire: dict[str, Any] = {}

    def capture(request: Any, **_: Any) -> None:
        wire.update(json.loads(request.body))

    client.meta.events.register("before-send.bedrock-runtime.Converse", capture)
    start = time.monotonic()
    try:
        resp = client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": "Reply with exactly: pong"}]}],
        )
    except Exception as exc:  # report and continue with the next profile
        print(f"[{model_id}] FAILED: {type(exc).__name__}: {exc}")
        return False
    elapsed = time.monotonic() - start
    text = "".join(b.get("text", "") for b in resp["output"]["message"]["content"])
    usage = resp.get("usage", {})
    problems = check_wire(wire)
    print(
        f"[{model_id}] region={region} stopReason={resp['stopReason']} "
        f"latency={elapsed:.2f}s tokens(in/out)={usage.get('inputTokens')}/"
        f"{usage.get('outputTokens')} reply={text.strip()!r}"
    )
    print(f"[{model_id}] wire request keys={sorted(wire)} forbidden-params={problems or 'none'}")
    return not problems and "pong" in text.lower()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ping Opus 5.5 on Bedrock with no inference params"
    )
    parser.add_argument("--profile", choices=["global", "us", "both"], default="both")
    args = parser.parse_args()
    region = os.environ.get("AWS_REGION", "us-east-1")
    prefixes = ["global", "us"] if args.profile == "both" else [args.profile]
    results = [ping(p, region) for p in prefixes]
    print("bedrock-ping:", "OK" if all(results) else "FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
