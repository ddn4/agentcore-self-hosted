"""Offline wire check: the exact Converse request the pinned model would send.

The live version is ``spikes/opus_params.py`` step 1 (needs AWS). This test
builds the Worker's pinned model (``config.bedrock_model``), calls
``model.stream(...)`` exactly as the plugin's ``invoke_model`` Activity does
(``temporalio/strands_agents/_model_activity.py::_stream``), and catches the
signed HTTP request in a botocore ``before-send`` hook. The hook then aborts,
so nothing leaves the machine and dummy credentials are enough.

It proves what we send, not what Bedrock accepts: the 400 checks for
temperature / disabled thinking / forced tool_choice stay live-only.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from temporalio.strands_agents.workflow import activity_as_tool

from support_agent import config
from support_agent.activities import get_return_policy, lookup_order
from support_agent.workflows import SUPERVISOR_PROMPT

pytestmark = pytest.mark.unit


class _Captured(Exception):
    """Raised from before-send so the request never reaches the network."""


@pytest.fixture(autouse=True)
def dummy_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "BEDROCK_MODEL_ID", "BEDROCK_EFFORT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAOFFLINETEST00000")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "offline-test-not-a-secret")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


async def wire_request(tool_choice: Any = None) -> tuple[str, dict[str, Any]]:
    """(URL, JSON body) of the first ConverseStream request the pinned model builds."""
    model = config.bedrock_model()
    seen: list[tuple[str, dict[str, Any]]] = []

    def capture(request: Any, **_: Any) -> None:
        seen.append((request.url, json.loads(request.body)))
        raise _Captured

    model.client.meta.events.register("before-send.bedrock-runtime.ConverseStream", capture)
    tool_specs = [activity_as_tool(fn).tool_spec for fn in (lookup_order, get_return_policy)]
    with pytest.raises(_Captured):
        async for _ in model.stream(
            [{"role": "user", "content": [{"text": "Customer ticket: order #1042 broke"}]}],
            tool_specs,
            SUPERVISOR_PROMPT,
            tool_choice=tool_choice,
        ):
            pass
    assert len(seen) == 1
    return seen[0]


async def test_default_request_has_pinned_model_no_sampling_params_and_auto_tool_choice() -> None:
    url, body = await wire_request()
    # The model ID is in the URL path for Converse: /model/<id>/converse-stream.
    assert "/model/global.anthropic.claude-opus-5-5/converse-stream" in url
    assert body.get("inferenceConfig", {}) == {}
    assert body["toolConfig"]["toolChoice"] == {"auto": {}}
    assert [t["toolSpec"]["name"] for t in body["toolConfig"]["tools"]] == [
        "lookup_order",
        "get_return_policy",
    ]
    # medium is the model default: no effort, no thinking config, nothing extra.
    assert "additionalModelRequestFields" not in body


async def test_effort_low_is_sent_as_output_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEDROCK_EFFORT", "low")
    _, body = await wire_request()
    assert body["additionalModelRequestFields"] == {"output_config": {"effort": "low"}}
    assert body.get("inferenceConfig", {}) == {}


async def test_forced_tool_choice_passes_straight_through() -> None:
    """Why ADR-0001 section 6 avoids structured_output_model on Opus 5.5.

    If Strands ever asks for a forced tool (its structured-output retry), the
    pinned model puts it on the wire unchanged; Opus 5.5 refuses that.
    """
    _, body = await wire_request(tool_choice={"tool": {"name": "lookup_order"}})
    assert body["toolConfig"]["toolChoice"] == {"tool": {"name": "lookup_order"}}
