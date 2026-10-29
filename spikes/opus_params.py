"""Spike (v0.2): what Strands + the Temporal plugin send to Opus 5.5, and effort.

    RUN_BEDROCK_TESTS=1 uv run python spikes/opus_params.py [--turns 3]

1. **Wire check.** One Supervisor turn through the plugin's ``invoke_model``
   Activity with the Worker's pinned factory and both tool specs. A botocore
   ``before-send`` hook asserts the real ConverseStream request has the pinned
   ``modelId``, no ``inferenceConfig.temperature/topP/topK``, no thinking
   config, and ``toolChoice`` ``auto`` (never forced).
2. **Rejections.** The same turn with ``temperature=0.5``, and with
   ``thinking: {type: disabled}``: both must be refused with a 400 (refused
   requests cost no tokens).
3. **Effort field.** ``additionalModelRequestFields.output_config.effort``:
   ``low`` must be accepted and a bogus level refused (proving the field is
   read, not ignored).
4. **Effort latency.** ``--turns`` turns each at ``low`` and ``medium``; prints
   per-turn latency and output tokens (recorded in docs/versions.md).

Read-only: invokes a model, creates no AWS resources. About 2 + 2N small calls.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys

from _wire import WireTap, invoke_model, user, wire_problems
from strands.models import BedrockModel
from temporalio.strands_agents.workflow import activity_as_tool

from support_agent import config
from support_agent.activities import get_return_policy, lookup_order
from support_agent.workflows import SUPERVISOR_PROMPT

TICKET = "Customer ticket: Order #1042, my headphones arrived broken. I want my money back."
TOOL_SPECS = [dict(activity_as_tool(fn).tool_spec) for fn in (lookup_order, get_return_policy)]


def pinned(**overrides: object) -> BedrockModel:
    model = config.bedrock_model()
    if overrides:
        model.update_config(**overrides)  # type: ignore[arg-type]
    return model


def main() -> int:
    if os.environ.get("RUN_BEDROCK_TESTS") != "1":
        print("set RUN_BEDROCK_TESTS=1 (calls Bedrock, costs tokens)")
        return 2
    parser = argparse.ArgumentParser()
    parser.add_argument("--turns", type=int, default=3)
    args = parser.parse_args()
    ok = True

    print(
        f"== 1. wire check: model_id={config.model_id()} region={config.aws_region()} "
        f"effort={config.effort()}"
    )
    tap = WireTap()
    turn = invoke_model(
        lambda: tap.attach(pinned()),
        user(TICKET),
        system_prompt=SUPERVISOR_PROMPT,
        tool_specs=TOOL_SPECS,
    )
    body = tap.last
    problems = wire_problems(body)
    print(
        f"   ok={turn.ok} stop={turn.stop_reason} tools={[t['name'] for t in turn.tool_uses]} "
        f"latency={turn.seconds:.2f}s usage={turn.usage} {turn.error}"
    )
    print(
        f"   wire: modelId={body.get('modelId')!r} keys={sorted(body)} "
        f"inferenceConfig={body.get('inferenceConfig')} "
        f"toolChoice={body.get('toolConfig', {}).get('toolChoice')} "
        f"additionalModelRequestFields={body.get('additionalModelRequestFields')}"
    )
    print(f"   forbidden on the wire: {problems or 'none'}")
    ok &= turn.ok and not problems and body.get("modelId") == config.model_id()
    ok &= turn.stop_reason == "tool_use"

    print("== 2. rejections (expect 400, no tokens)")
    for label, factory in [
        ("temperature=0.5", lambda: pinned(temperature=0.5)),
        (
            "thinking disabled",
            lambda: pinned(additional_request_fields={"thinking": {"type": "disabled"}}),
        ),
    ]:
        rejected = invoke_model(factory, user("Reply with: pong"))
        print(f"   {label}: accepted={rejected.ok} {rejected.error[:200]}")
        ok &= not rejected.ok

    print("== 3. effort field: additionalModelRequestFields.output_config.effort")
    low = invoke_model(
        lambda: pinned(additional_request_fields=config.effort_fields("low")),
        user("Reply with: pong"),
    )
    bogus = invoke_model(
        lambda: pinned(additional_request_fields={"output_config": {"effort": "bogus"}}),
        user("Reply with: pong"),
    )
    print(f"   low accepted={low.ok} reply={low.text.strip()!r} {low.error[:200]}")
    print(f"   bogus accepted={bogus.ok} {bogus.error[:200]}")
    ok &= low.ok and not bogus.ok

    print(f"== 4. effort latency: {args.turns} Supervisor turns each")
    for level in ("low", "medium"):
        times, outs = [], []
        for _ in range(args.turns):
            t = invoke_model(
                lambda lv=level: pinned(additional_request_fields=config.effort_fields(lv)),
                user(TICKET),
                system_prompt=SUPERVISOR_PROMPT,
                tool_specs=TOOL_SPECS,
            )
            ok &= t.ok
            times.append(t.seconds)
            outs.append(t.usage.get("outputTokens", 0))
        print(
            f"   {level:6} latency s: {', '.join(f'{x:.2f}' for x in times)} "
            f"(median {statistics.median(times):.2f}); output tokens: {outs}"
        )

    print("opus_params:", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
