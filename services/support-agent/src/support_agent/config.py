"""Configuration for team Support (caller).

Each service owns its own config module, on purpose: the only code the two teams
share is `finance_a2a_api` (IMPLEMENTATION_PLAN.md section 2.2, layout rules).

Precedence (plan section 3.2): runtime-injected env > .env (exported by make) >
the `TEMPORAL_PROFILE` section of `TEMPORAL_CONFIG_FILE` > code defaults here.
The first three are handled by `temporalio.envconfig`; this module only fills
in the code defaults.
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path
from typing import Any, Final

from botocore.config import Config as BotocoreConfig
from strands.models import BedrockModel
from strands.models.bedrock import DEFAULT_READ_TIMEOUT
from temporalio.client import Client, Plugin
from temporalio.envconfig import ClientConfig, ClientConnectConfig
from temporalio.strands_agents import StrandsPlugin

from finance_a2a_api import APPROVAL_WAIT_LIMIT
from support_agent.durations import format_duration, parse_duration

#: Namespace this service's Workers poll.
NAMESPACE: Final[str] = "agents"
#: Model key every `TemporalAgent` in this service uses (`model="bedrock"`).
MODEL_KEY: Final[str] = "bedrock"
DEFAULT_MODEL_ID: Final[str] = "global.anthropic.claude-opus-5-5"
DEFAULT_AWS_REGION: Final[str] = "us-east-1"
DEFAULT_ADDRESS: Final[str] = "127.0.0.1:7233"
#: Opus 5.5 effort levels (ADR-0001). ``medium`` is the model's own default.
EFFORT_LEVELS: Final[tuple[str, ...]] = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT: Final[str] = "medium"


def task_queue() -> str:
    """Task queue for this service's local, unversioned Worker (``make worker-support``)."""
    return os.environ.get("SUPPORT_TASK_QUEUE", "support-dev-tq")


# --- v0.7: the serverless (AgentCore) shape --------------------------------------------
# Names follow the AgentCore runtime env of runbook 5.4 and samples-python
# bedrock_agentcore/strands_agent (TEMPORAL_TASK_QUEUE / _DEPLOYMENT_NAME / _BUILD_ID).

#: Worker Deployment of the Support Supervisor (plan 3.3, ADR-0004).
DEPLOYMENT_NAME: Final[str] = "support-supervisor"
#: Versioned task queue polled only by serverless-shape Workers (plan 3.3).
VERSIONED_TASK_QUEUE: Final[str] = "support-tq"
#: Debounce before an idle AgentCore-shaped Worker drains (plan 3.3: local 20 s).
DEFAULT_DEBOUNCE_SECONDS: Final[float] = 20.0
#: Fail fast when the Temporal frontend is unreachable (plan 3.2).
DEFAULT_CONNECT_TIMEOUT_SECONDS: Final[float] = 10.0
#: How long a drain waits for in-flight Activities (runbook 5.2; the sample's DRAIN).
GRACEFUL_SHUTDOWN_TIMEOUT: Final[timedelta] = timedelta(seconds=120)


class MissingBuildIdError(RuntimeError):
    """A versioned Worker refuses to start without an explicit Build ID."""


def versioned_task_queue() -> str:
    """``TEMPORAL_TASK_QUEUE`` (default ``support-tq``).

    Deliberately *not* ``SUPPORT_TASK_QUEUE``: ``.env`` sets that to the unversioned
    dev queue, and a versioned Worker must never poll it.
    """
    return os.environ.get("TEMPORAL_TASK_QUEUE", "").strip() or VERSIONED_TASK_QUEUE


def deployment_name() -> str:
    """``TEMPORAL_DEPLOYMENT_NAME`` (default ``support-supervisor``)."""
    return os.environ.get("TEMPORAL_DEPLOYMENT_NAME", "").strip() or DEPLOYMENT_NAME


def build_id() -> str:
    """``TEMPORAL_BUILD_ID``, else the ``BUILD_ID`` file next to the code (CodeZip bundle).

    There is no default on purpose (as in samples-python's AgentCore README): a made-up
    Build ID would register a version nothing routes to, and PINNED Workflows would
    strand on it. Locally the Makefile passes ``scripts/build-id.sh``'s output.
    """
    value = os.environ.get("TEMPORAL_BUILD_ID", "").strip()
    if value:
        return value
    for path in _build_id_files():
        if path.is_file():
            text = path.read_text().strip()
            if text:
                return text
    raise MissingBuildIdError(
        "TEMPORAL_BUILD_ID is not set and no BUILD_ID file was found; "
        "run scripts/build-id.sh (make build-id) or set it in the runtime env"
    )


def _build_id_files() -> list[Path]:
    here = Path(__file__).resolve().parent
    # Bundle layout (make agentcore-bundle): <root>/BUILD_ID, <root>/support_agent/config.py
    return [here.parent / "BUILD_ID", here / "BUILD_ID"]


def _positive_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{name}={raw!r} must be positive")
    return value


def debounce_seconds() -> float:
    """``AGENTCORE_DEBOUNCE_SECONDS`` (default 20 locally; runbook default 60, cloud 30)."""
    return _positive_float("AGENTCORE_DEBOUNCE_SECONDS", DEFAULT_DEBOUNCE_SECONDS)


def connect_timeout_seconds() -> float:
    """``TEMPORAL_CONNECT_TIMEOUT_SECONDS`` (default 10)."""
    return _positive_float("TEMPORAL_CONNECT_TIMEOUT_SECONDS", DEFAULT_CONNECT_TIMEOUT_SECONDS)


#: Headroom above Finance's published approval limit (ADR-0009): 24 h + 1 h = 25 h.
NEXUS_BUDGET_HEADROOM: Final[timedelta] = timedelta(hours=1)
#: Default caller budget for one A2A task (``SUPPORT_NEXUS_SEND_TIMEOUT``).
DEFAULT_NEXUS_SEND_TIMEOUT: Final[str] = format_duration(
    APPROVAL_WAIT_LIMIT + NEXUS_BUDGET_HEADROOM
)


def nexus_send_timeout() -> str:
    """Support's ``send_message`` budget (``SUPPORT_NEXUS_SEND_TIMEOUT``, default 25h).

    Text, because it is passed to ``SupportSupervisorWorkflow`` as its third input
    (Workflow code can't read the environment; as an input it is in history). The
    starter (``scripts/demo/case-a.sh``, the e2e test) reads it from here.
    """
    raw = os.environ.get("SUPPORT_NEXUS_SEND_TIMEOUT", "").strip() or DEFAULT_NEXUS_SEND_TIMEOUT
    parse_duration(raw)  # fail fast on a typo
    return raw


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
