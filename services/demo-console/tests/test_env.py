"""``Settings.child_env`` (DESIGN.md 4.2, 4.4): the Makefile's unexport, mirrored."""

from __future__ import annotations

from pathlib import Path

import pytest

from demo_console.fakes import make_settings
from demo_console.settings import Settings

pytestmark = pytest.mark.unit


def test_local_keeps_plaintext(tmp_path: Path) -> None:
    env = make_settings(tmp_path).child_env("4ee7f87")
    assert env["TEMPORAL_PROFILE"] == "local"
    assert env["TEMPORAL_TLS"] == "false" and env["TEMPORAL_ADDRESS"] == "127.0.0.1:7233"
    assert env["TEMPORAL_BUILD_ID"] == "4ee7f87"
    assert Path(env["TEMPORAL_CONFIG_FILE"]).is_absolute()
    assert env["TEMPORAL_CONFIG_FILE"] == str(tmp_path / "config" / "temporal.toml")


@pytest.mark.parametrize("profile", ["kind", "kind-plaintext", "eks-pf"])
def test_other_profiles_strip_the_dev_server_vars(tmp_path: Path, profile: str) -> None:
    settings = make_settings(tmp_path, profile=profile, TEMPORAL_NAMESPACE="default")
    for env in (settings.child_env(None), settings.temporal_env()):
        assert env["TEMPORAL_PROFILE"] == profile
        assert "TEMPORAL_TLS" not in env and "TEMPORAL_ADDRESS" not in env
        assert "TEMPORAL_NAMESPACE" not in env
        assert "TEMPORAL_BUILD_ID" not in env


def test_relative_config_file_is_made_absolute(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, TEMPORAL_CONFIG_FILE="config/temporal.toml")
    assert settings.config_file == tmp_path / "config" / "temporal.toml"


def test_from_env_precedence(tmp_path: Path) -> None:
    s = Settings.from_env(root=tmp_path, env={"TEMPORAL_PROFILE": "kind", "CONSOLE_ALLOW_AWS": "1"})
    assert s.profile == "kind" and s.allow_aws and s.port == 8099
    assert Settings.from_env(root=tmp_path, profile="eks-pf", env={}).profile == "eks-pf"
    assert Settings.from_env(root=tmp_path, env={}).profile == "local"
    assert Settings.from_env(root=tmp_path, profile="eks-pf", env={}).debounce_s == 30.0
    assert (
        Settings.from_env(root=tmp_path, env={"AGENTCORE_DEBOUNCE_SECONDS": "5"}).debounce_s == 5.0
    )
    with pytest.raises(ValueError, match="unknown profile"):
        Settings.from_env(root=tmp_path, env={"TEMPORAL_PROFILE": "prod"})


def test_config_body(tmp_path: Path) -> None:
    body = make_settings(tmp_path).config_body()
    assert body["profiles"] == ["local", "kind", "kind-plaintext", "eks-pf"]
    assert body["ui_base"] == "http://127.0.0.1:8233" and body["allow_aws"] is False
    assert body["default_approver"] == "dana.finance" and body["auto_approve_limit_usd"] == 100


def test_parent_make_state_never_reaches_children(tmp_path: Path) -> None:
    """`make console TEMPORAL_PROFILE=kind` must not pin every child make to kind."""
    settings = make_settings(
        tmp_path,
        MAKEFLAGS=" -- TEMPORAL_PROFILE=kind",
        MFLAGS="",
        MAKELEVEL="1",
        MAKEOVERRIDES="${-*-command-variables-*-}",
    )
    env = settings.child_env("4ee7f87")
    assert not {"MAKEFLAGS", "MFLAGS", "MAKELEVEL", "MAKEOVERRIDES"} & env.keys()
    assert env["TEMPORAL_PROFILE"] == "local"
