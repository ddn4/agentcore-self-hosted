"""Unit tests: model pin, plugin import path, envconfig precedence."""

from pathlib import Path

import pytest
from strands.models import BedrockModel
from temporalio.strands_agents import StrandsPlugin

from finance_agent import config

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_FILE = REPO_ROOT / "config" / "temporal.toml"

_ENV_KEYS = (
    "TEMPORAL_CONFIG_FILE",
    "TEMPORAL_PROFILE",
    "TEMPORAL_ADDRESS",
    "TEMPORAL_NAMESPACE",
    "TEMPORAL_TLS",
    "BEDROCK_MODEL_ID",
    "BEDROCK_EFFORT",
    "AWS_REGION",
    "FINANCE_TASK_QUEUE",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    # Never fall back to the developer's ~/.config/temporalio/temporal.toml.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


def test_plugin_import_path_and_pinned_model_key() -> None:
    plugin = config.build_strands_plugin()
    assert isinstance(plugin, StrandsPlugin)
    assert config.MODEL_KEY == "bedrock"


def test_model_defaults_to_opus_global_profile_in_us_east_1() -> None:
    model = config.bedrock_model()
    assert isinstance(model, BedrockModel)
    cfg = model.get_config()
    assert cfg.get("model_id") == "global.anthropic.claude-opus-5-5"
    assert model.client.meta.region_name == "us-east-1"
    # No inference params are configured on the model (Opus 5.5 rejects them).
    for forbidden in ("temperature", "top_p", "top_k"):
        assert forbidden not in cfg
    assert "thinking" not in str(cfg.get("additional_request_fields", ""))


def test_model_id_and_region_follow_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEDROCK_MODEL_ID", "us.anthropic.claude-opus-5-5")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    model = config.bedrock_model()
    assert model.get_config().get("model_id") == "us.anthropic.claude-opus-5-5"
    assert model.client.meta.region_name == "us-west-2"


def test_code_defaults_without_file_or_env() -> None:
    cfg = config.connect_config()
    assert cfg.get("target_host") == "127.0.0.1:7233"
    assert cfg.get("namespace") == "agent-services"
    assert config.task_queue() == "agent-services-tq"


def test_local_profile_from_config_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEMPORAL_CONFIG_FILE", str(CONFIG_FILE))
    monkeypatch.setenv("TEMPORAL_PROFILE", "local")
    cfg = config.connect_config()
    assert cfg.get("target_host") == "127.0.0.1:7233"
    assert cfg.get("tls") is False
    # The profile names no namespace, so each service lands in its own.
    assert cfg.get("namespace") == "agent-services"


def test_env_overrides_config_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEMPORAL_CONFIG_FILE", str(CONFIG_FILE))
    monkeypatch.setenv("TEMPORAL_PROFILE", "local")
    monkeypatch.setenv("TEMPORAL_ADDRESS", "10.0.0.1:7233")
    monkeypatch.setenv("TEMPORAL_NAMESPACE", "elsewhere")
    cfg = config.connect_config()
    assert cfg.get("target_host") == "10.0.0.1:7233"
    assert cfg.get("namespace") == "elsewhere"


def test_default_effort_sends_no_extra_fields() -> None:
    # medium is Opus 5.5's own default: the request stays minimal (ADR-0001).
    assert config.effort() == "medium"
    assert config.bedrock_model().get_config().get("additional_request_fields") is None


def test_effort_goes_in_output_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEDROCK_EFFORT", "low")
    fields = config.bedrock_model().get_config().get("additional_request_fields")
    assert fields == {"output_config": {"effort": "low"}}


def test_unknown_effort_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEDROCK_EFFORT", "turbo")
    with pytest.raises(ValueError, match="BEDROCK_EFFORT"):
        config.bedrock_model()
