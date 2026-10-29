"""Configuration for team Finance (handler).

Each service owns its own config module, on purpose: the only code the two teams
share is `finance_a2a_api` (IMPLEMENTATION_PLAN.md section 2.2, layout rules).

Precedence (plan section 3.2): runtime-injected env > .env (exported by make) >
the `TEMPORAL_PROFILE` section of `TEMPORAL_CONFIG_FILE` > code defaults here.
The first three are handled by `temporalio.envconfig`; this module only fills
in the code defaults.
"""

from __future__ import annotations

import os
from typing import Any, Final

from botocore.config import Config as BotocoreConfig
from strands.models import BedrockModel
from strands.models.bedrock import DEFAULT_READ_TIMEOUT
from temporalio.client import Client, Plugin
from temporalio.envconfig import ClientConfig, ClientConnectConfig
from temporalio.strands_agents import StrandsPlugin

#: Namespace this service's Workers poll.
NAMESPACE: Final[str] = "agent-services"
#: Model key every `TemporalAgent` in this service uses (`model="bedrock"`).
MODEL_KEY: Final[str] = "bedrock"
DEFAULT_MODEL_ID: Final[str] = "global.anthropic.claude-opus-5-5"
DEFAULT_AWS_REGION: Final[str] = "us-east-1"
DEFAULT_ADDRESS: Final[str] = "127.0.0.1:7233"
#: Opus 5.5 effort levels (ADR-0001). ``medium`` is the model's own default.
EFFORT_LEVELS: Final[tuple[str, ...]] = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT: Final[str] = "medium"


def task_queue() -> str:
    """Task queue for this service's Worker."""
    return os.environ.get("FINANCE_TASK_QUEUE", "agent-services-tq")


def model_id() -> str:
    return os.environ.get("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)


def aws_region() -> str:
    return os.environ.get("AWS_REGION", DEFAULT_AWS_REGION)


def effort() -> str:
    """Effort level from ``BEDROCK_EFFORT`` (default ``medium``)."""
    level = os.environ.get("BEDROCK_EFFORT", DEFAULT_EFFORT).strip().lower()
    if level not in EFFORT_LEVELS:
        raise ValueError(f"BEDROCK_EFFORT={level!r}; expected one of {EFFORT_LEVELS}")
    return level


def effort_fields(level: str) -> dict[str, Any] | None:
    """Bedrock ``additionalModelRequestFields`` for an effort level (ADR-0001).

    Opus 5.5 takes effort as ``output_config.effort`` in the Anthropic request
    body, which Converse forwards from ``additionalModelRequestFields``. At the
    model default (``medium``) nothing is sent, so the default request carries
    only messages, system and tools.
    """
    if level == DEFAULT_EFFORT:
        return None
    return {"output_config": {"effort": level}}


def bedrock_model() -> BedrockModel:
    """The pinned model (ADR-0001).

    Never pass temperature, top_p, top_k, a disabled-thinking setting or a forced
    tool_choice: Opus 5.5 on Bedrock rejects them with a 400. Latency is traded
    with effort only (``BEDROCK_EFFORT``). Botocore retries
    are disabled, as in the plugin's own default factory, because Temporal owns
    retries of the `invoke_model` Activity.
    """
    return BedrockModel(
        model_id=model_id(),
        region_name=aws_region(),
        additional_request_fields=effort_fields(effort()),
        boto_client_config=BotocoreConfig(
            read_timeout=DEFAULT_READ_TIMEOUT,
            retries={"max_attempts": 0},
        ),
    )


def build_strands_plugin() -> StrandsPlugin:
    """StrandsPlugin with the model pinned under the `"bedrock"` key.

    Without a `models` mapping the plugin would use Strands' default
    `BedrockModel()`, which is a different model. Tests register a scripted
    model under the same key, so the pin is also the test seam.
    """
    return StrandsPlugin(models={MODEL_KEY: bedrock_model})


def connect_config() -> ClientConnectConfig:
    """Client.connect kwargs from envconfig, with this service's code defaults."""
    config = ClientConfig.load_client_connect_config()
    config.setdefault("target_host", DEFAULT_ADDRESS)
    config.setdefault("namespace", NAMESPACE)
    return config


async def connect(plugins: list[Plugin] | None = None) -> Client:
    """Connect to Temporal using envconfig (TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE / env)."""
    return await Client.connect(**connect_config(), plugins=plugins or [])
