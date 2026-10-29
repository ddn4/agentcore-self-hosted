"""Shared helpers for the Opus 5.5 spikes (v0.2).

``invoke_model`` runs the Strands plugin's own ``invoke_model`` Activity (the
code every Worker runs for each LLM turn) inside an ``ActivityEnvironment``,
with the model factory a Worker pins (``support_agent.config.bedrock_model``).
So what these spikes put on the wire is exactly what the Worker puts on the
wire, without needing a Temporal server.

``WireTap`` registers a botocore ``before-send`` hook on the model's client and
keeps every Converse/ConverseStream request body.

Note: ``_model_activity`` is a private module of temporalio-strands-agents
0.1.0. Spikes may use it; service code must not.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from strands.models import BedrockModel, Model
from temporalio.strands_agents._model_activity import ModelActivity, _InvokeModelInput
from temporalio.testing import ActivityEnvironment

FORBIDDEN_INFERENCE = ("temperature", "topP", "topK")
FORBIDDEN_FIELDS = ("temperature", "top_p", "top_k", "thinking")


def aws_session_ok(region: str) -> bool:
    """True if the default credential chain works (STS read-only call)."""
    try:
        boto3.client("sts", region_name=region).get_caller_identity()
        return True
    except (BotoCoreError, ClientError) as err:
        print(f"AWS credentials unusable ({type(err).__name__}): run `aws sso login` and retry")
        return False


@dataclass
class WireTap:
    requests: list[dict[str, Any]] = field(default_factory=list)

    def attach(self, model: BedrockModel) -> BedrockModel:
        events = model.client.meta.events
        for op in ("Converse", "ConverseStream"):
            events.register(f"before-send.bedrock-runtime.{op}", self._capture)
        return model

    def _capture(self, request: Any, **_: Any) -> None:
        self.requests.append(json.loads(request.body))

    @property
    def last(self) -> dict[str, Any]:
        return self.requests[-1] if self.requests else {}


def wire_problems(body: dict[str, Any]) -> list[str]:
    """Anything Opus 5.5 would reject (or we promised never to send)."""
    problems = [
        f"inferenceConfig.{k}" for k in FORBIDDEN_INFERENCE if k in body.get("inferenceConfig", {})
    ]
    extra = json.dumps(body.get("additionalModelRequestFields", {}))
    problems += [
        f"additionalModelRequestFields has {k!r}" for k in FORBIDDEN_FIELDS if f'"{k}"' in extra
    ]
    choice = body.get("toolConfig", {}).get("toolChoice")
    if choice is not None and set(choice) - {"auto"}:
        problems.append(f"forced toolChoice {choice}")
    return problems


@dataclass
class Turn:
    ok: bool
    seconds: float
    stop_reason: str | None = None
    text: str = ""
    tool_uses: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    error: str = ""


def invoke_model(
    factory: Callable[[], Model],
    messages: list[dict[str, Any]],
    *,
    system_prompt: str | None = None,
    tool_specs: list[dict[str, Any]] | None = None,
    tool_choice: dict[str, Any] | None = None,
) -> Turn:
    """One LLM turn through the plugin's invoke_model Activity."""
    activity = ModelActivity({"bedrock": factory})
    start = time.monotonic()
    try:
        events = ActivityEnvironment().run(
            activity.invoke_model,
            _InvokeModelInput(
                model_name="bedrock",
                messages=messages,
                tool_specs=tool_specs,
                system_prompt=system_prompt,
                tool_choice=tool_choice,
            ),
        )
        events = _await(events)
    except ClientError as err:
        info = err.response.get("Error", {})
        return Turn(
            False, time.monotonic() - start, error=f"{info.get('Code')}: {info.get('Message')}"
        )
    except Exception as err:  # Strands wraps some ClientErrors
        return Turn(False, time.monotonic() - start, error=f"{type(err).__name__}: {err}")
    elapsed = time.monotonic() - start
    turn = Turn(True, elapsed)
    current: dict[str, Any] | None = None
    for event in events:
        if "contentBlockStart" in event:
            start_block = event["contentBlockStart"].get("start", {})
            if "toolUse" in start_block:
                current = {"name": start_block["toolUse"]["name"], "input": ""}
                turn.tool_uses.append(current)
        elif "contentBlockDelta" in event:
            delta = event["contentBlockDelta"]["delta"]
            turn.text += delta.get("text", "")
            if "toolUse" in delta and current is not None:
                current["input"] += delta["toolUse"].get("input", "")
        elif "contentBlockStop" in event:
            current = None
        elif "messageStop" in event:
            turn.stop_reason = event["messageStop"].get("stopReason")
        elif "metadata" in event:
            turn.usage = event["metadata"].get("usage", {})
    for use in turn.tool_uses:
        use["input"] = json.loads(use["input"] or "{}")
    return turn


def _await(value: Any) -> Any:
    """ActivityEnvironment.run returns the coroutine of an async activity."""
    import asyncio
    import inspect

    if inspect.isawaitable(value):
        return asyncio.run(value)  # type: ignore[arg-type]
    return value


def user(text: str) -> list[dict[str, Any]]:
    return [{"role": "user", "content": [{"text": text}]}]
