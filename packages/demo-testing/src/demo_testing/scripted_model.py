"""ScriptedModel: an offline Strands model registered under the ``"bedrock"`` key.

The test seam (ADR-0001): services pin Opus 5.5 with
``StrandsPlugin(models={"bedrock": <factory>})``; tests build
the *same* plugin shape with a ``ScriptedModel`` under the same key, so the
pinning mechanism is also what makes ``make check`` run without AWS.

Shape follows samples-python ``tests/strands_plugin/_mock_model.py`` (itself
vendored from the SDK's ``tests/contrib/strands/mock_model.py``): each script
entry drives one ``stream()`` call; a ``str`` is a text turn and a tool call is
a tool-use turn. The differences:

- it is injected through ``models={...}`` instead of monkeypatching the
  plugin's ``BedrockModel`` (that only works for the plugin's implicit default);
- a turn may hold several tool calls (parallel tool use);
- :class:`Hang` blocks the call until it is cancelled, to simulate a Worker that
  dies while the model is "thinking" (the crash-safety test);
- a script may be a function of the request, so later tags (nested agents,
  v0.3) can answer per system prompt;
- every request is recorded in :attr:`ScriptedModel.calls` for assertions
  (system prompt, tool names, ``tool_choice``).

The plugin builds each model once per Worker and caches it (``ModelActivity``),
so one ``ScriptedModel`` instance serves every ``invoke_model`` Activity the
Worker runs and its script is consumed across them.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from strands.models import Model
from strands.types.streaming import StreamEvent
from temporalio.client import Client
from temporalio.strands_agents import StrandsPlugin

#: Model key every TemporalAgent in this repo uses (ADR-0001).
MODEL_KEY = "bedrock"


@dataclass(frozen=True)
class ToolCall:
    """One tool use requested by the scripted model."""

    name: str
    input: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Hang:
    """Block this model call until the Activity is cancelled (simulated crash)."""


Turn = str | ToolCall | Sequence[ToolCall] | Hang


@dataclass(frozen=True)
class ModelCall:
    """What one ``invoke_model`` Activity asked the model."""

    system_prompt: str | None
    tool_names: list[str]
    tool_choice: Any
    messages: list[Any]

    @property
    def last_user_text(self) -> str:
        """Text of the most recent user message (tool results excluded)."""
        for message in reversed(self.messages):
            if message.get("role") != "user":
                continue
            texts = [block["text"] for block in message.get("content", []) if "text" in block]
            if texts:
                return "\n".join(texts)
        return ""

    @property
    def tool_results(self) -> list[str]:
        """Text of every tool result in the conversation so far, oldest first."""
        out: list[str] = []
        for message in self.messages:
            for block in message.get("content", []):
                result = block.get("toolResult")
                if result:
                    out.extend(c["text"] for c in result.get("content", []) if "text" in c)
        return out


Responder = Callable[[ModelCall], Turn]


class ScriptExhaustedError(AssertionError):
    """The workflow asked the model more often than the test scripted."""


class ScriptedModel(Model):
    """A deterministic Strands ``Model`` driven by a script or a responder function."""

    def __init__(self, script: Sequence[Turn] | Responder, *, delay: float = 0.0) -> None:
        """``delay`` seconds of simulated "thinking" before each answer (crash demos)."""
        self._delay = delay
        if callable(script):
            self._responder: Responder | None = script
            self._script: list[Turn] = []
        else:
            self._responder = None
            self._script = list(script)
        self._tool_use_counter = 0
        self.calls: list[ModelCall] = []
        #: Set when a :class:`Hang` turn starts (the simulated crash point).
        self.hung = asyncio.Event()

    # Strands Model interface -------------------------------------------------

    def update_config(self, **_model_config: Any) -> None:
        return None

    def get_config(self) -> dict[str, Any]:
        return {"model_id": "scripted"}

    def structured_output(self, *_args: Any, **_kwargs: Any) -> Any:
        raise NotImplementedError("use a tool call named after the output model instead")

    async def stream(
        self,
        messages: Any,
        tool_specs: Any = None,
        system_prompt: str | None = None,
        *,
        tool_choice: Any = None,
        **_kwargs: Any,
    ) -> AsyncIterable[StreamEvent]:
        call = ModelCall(
            system_prompt=system_prompt,
            tool_names=[spec["name"] for spec in tool_specs or []],
            tool_choice=tool_choice,
            messages=list(messages),
        )
        self.calls.append(call)
        turn = self._next_turn(call)

        if isinstance(turn, Hang):
            self.hung.set()
            await asyncio.Event().wait()
        if self._delay:
            await asyncio.sleep(self._delay)  # until the Activity is cancelled

        yield {"messageStart": {"role": "assistant"}}
        if isinstance(turn, str):
            yield {"contentBlockDelta": {"delta": {"text": turn}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
            return

        calls = [turn] if isinstance(turn, ToolCall) else list(turn)  # type: ignore[arg-type]
        for tool_call in calls:
            self._tool_use_counter += 1
            yield {
                "contentBlockStart": {
                    "start": {
                        "toolUse": {
                            "name": tool_call.name,
                            "toolUseId": f"scripted-tool-{self._tool_use_counter}",
                        }
                    }
                }
            }
            yield {
                "contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(tool_call.input)}}}
            }
            yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use"}}

    # -------------------------------------------------------------------------

    def _next_turn(self, call: ModelCall) -> Turn:
        if self._responder is not None:
            return self._responder(call)
        if not self._script:
            raise ScriptExhaustedError(
                f"ScriptedModel script exhausted after {len(self.calls) - 1} calls"
            )
        return self._script.pop(0)


def scripted_plugin(model: ScriptedModel) -> StrandsPlugin:
    """A StrandsPlugin whose ``"bedrock"`` model is ``model`` (the services' shape)."""
    return StrandsPlugin(models={MODEL_KEY: lambda: model})


def client_with_plugin(client: Client, plugin: StrandsPlugin) -> Client:
    """``client`` plus ``plugin``, as samples-python ``tests/strands_plugin/*_test.py`` do.

    The plugin must be on the client (not only the Worker) so the pydantic data
    converter and the Strands failure converter are used end to end.
    """
    config = client.config()
    config["plugins"] = [*config["plugins"], plugin]
    return Client(**config)
