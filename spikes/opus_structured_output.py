"""Spike (v0.2): structured output vs forced tool_choice on Opus 5.5.

    RUN_BEDROCK_TESTS=1 uv run python spikes/opus_structured_output.py

``TemporalAgent(structured_output_model=X)`` (samples-python
``strands_plugin/structured_output``) adds an ``X`` tool with ``toolChoice``
``auto``. If the model ends its turn *without* calling it, Strands retries with
``toolChoice {"tool": {"name": "X"}}`` (forced) and only that tool spec
(``strands/event_loop/event_loop.py``: ``set_forced_mode``). Plain Strands
falls back to native output / auto when Bedrock refuses forced tool use, but the
Temporal plugin's ``invoke_model`` Activity does not carry
``structured_output_model``, so through the plugin the forced request goes to
Bedrock as is. This spike measures each path on Opus 5.5:

A. raw Converse, forced ``toolChoice``                         (expect 400)
B. the plugin's invoke_model Activity, the forced retry request  (expect failure)
C. fallback 1: unforced capture tool ``submit_refund_request``,
   ``toolChoice`` auto, validated with pydantic                 (expect success)
D. fallback 2: JSON text validated with pydantic                (informational)

Read-only. About 3 model calls (A and B are refused before generation).
"""

from __future__ import annotations

import json
import os
import sys

import boto3
from _wire import WireTap, invoke_model, user
from botocore.config import Config
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field, ValidationError

from support_agent import config


class RefundRequest(BaseModel):
    """What the Supervisor hands to Finance in handoff mode (v0.5)."""

    order_id: str = Field(description="Order number, digits only")
    amount_usd: float = Field(description="Refund amount in USD")
    reason: str = Field(description="Why the customer is owed a refund")


SPEC = {
    "name": "RefundRequest",
    "description": "Record the refund request.",
    "inputSchema": {"json": RefundRequest.model_json_schema()},
}
CAPTURE_SPEC = {
    "name": "submit_refund_request",
    "description": "Submit the refund request for Finance. Call this exactly once.",
    "inputSchema": {"json": RefundRequest.model_json_schema()},
}
FACTS = (
    "Order 1042: Wireless headphones, $40.00, delivered 6 days ago, arrived damaged. "
    "Damaged items within 30 days are eligible."
)


def main() -> int:
    if os.environ.get("RUN_BEDROCK_TESTS") != "1":
        print("set RUN_BEDROCK_TESTS=1 (calls Bedrock, costs tokens)")
        return 2
    ok = True

    print("== A. raw Converse, toolChoice forced to one tool")
    client = boto3.client(
        "bedrock-runtime",
        region_name=config.aws_region(),
        config=Config(retries={"max_attempts": 0}),
    )
    try:
        client.converse(
            modelId=config.model_id(),
            messages=[
                {"role": "user", "content": [{"text": FACTS + " Record the refund request."}]}
            ],
            toolConfig={
                "tools": [{"toolSpec": SPEC}],
                "toolChoice": {"tool": {"name": "RefundRequest"}},
            },
        )
        print("   accepted (forced tool use is supported?)")
        forced_raw = True
    except ClientError as err:
        info = err.response["Error"]
        print(
            f"   refused: {err.response['ResponseMetadata']['HTTPStatusCode']} "
            f"{info['Code']}: {info['Message']}"
        )
        forced_raw = False
    for choice in ({"any": {}},):
        try:
            client.converse(
                modelId=config.model_id(),
                messages=[{"role": "user", "content": [{"text": "Record it."}]}],
                toolConfig={"tools": [{"toolSpec": SPEC}], "toolChoice": choice},
            )
            print(f"   toolChoice {choice}: accepted")
        except ClientError as err:
            print(f"   toolChoice {choice}: refused {err.response['Error']['Message']}")

    print("== B. plugin invoke_model Activity with Strands' forced-retry request")
    tap = WireTap()
    messages = [
        {"role": "user", "content": [{"text": FACTS}]},
        {"role": "assistant", "content": [{"text": "The refund is eligible."}]},
        {
            "role": "user",
            "content": [{"text": "You must format the previous response as structured output."}],
        },
    ]
    forced = invoke_model(
        lambda: tap.attach(config.bedrock_model()),
        messages,
        tool_specs=[SPEC],
        tool_choice={"tool": {"name": "RefundRequest"}},
    )
    print(
        f"   wire toolChoice={tap.last.get('toolConfig', {}).get('toolChoice')} "
        f"-> ok={forced.ok} {forced.error[:240]}"
    )
    ok &= not forced.ok and not forced_raw

    print("== C. fallback 1: unforced capture tool (toolChoice auto)")
    tap = WireTap()
    capture = invoke_model(
        lambda: tap.attach(config.bedrock_model()),
        user(FACTS + " File the refund request for Finance."),
        system_prompt="You are a support agent. To file a refund, call submit_refund_request.",
        tool_specs=[CAPTURE_SPEC],
    )
    print(
        f"   wire toolChoice={tap.last.get('toolConfig', {}).get('toolChoice')} "
        f"stop={capture.stop_reason} "
        f"latency={capture.seconds:.2f}s {capture.error}"
    )
    try:
        req = RefundRequest.model_validate(capture.tool_uses[0]["input"])
        print(f"   captured {req!r}")
        ok &= req.order_id == "1042" and abs(req.amount_usd - 40.0) < 0.01
    except (IndexError, ValidationError) as err:
        print(f"   no valid capture: {err}")
        ok = False

    print("== D. fallback 2: JSON text + pydantic (informational)")
    text = invoke_model(
        config.bedrock_model,
        user(
            FACTS + " Reply with only a JSON object with keys order_id (string), "
            "amount_usd (number) and reason (string). No prose, no code fence."
        ),
    )
    try:
        req = RefundRequest.model_validate(json.loads(text.text.strip()))
        print(f"   parsed {req!r} latency={text.seconds:.2f}s")
    except (ValueError, ValidationError) as err:
        print(f"   not parseable: {err} text={text.text[:120]!r}")

    print(
        "opus_structured_output:", "OK (forced refused; capture tool works)" if ok else "UNEXPECTED"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
