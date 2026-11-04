"""Console settings and the environment every child process gets (DESIGN.md 4.2, 4.4).

Precedence follows the Makefile: runtime env > ``.env`` (exported by make) > ``local``.
Outside ``local`` the dev server's ``TEMPORAL_TLS`` / ``TEMPORAL_ADDRESS`` from ``.env`` are
removed (the Makefile ``unexport``s them), or they would silently switch mTLS off.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from demo_console import __version__

PROFILES: Final[tuple[str, ...]] = ("local", "kind", "kind-plaintext")
#: Env vars the Makefile unexports outside ``local``.
LOCAL_ONLY_ENV: Final[tuple[str, ...]] = ("TEMPORAL_TLS", "TEMPORAL_ADDRESS")
#: A parent make's own state; never passed on to the console's ``make -s <target>`` children.
MAKE_ENV: Final[tuple[str, ...]] = ("MAKEFLAGS", "MFLAGS", "MAKELEVEL", "MAKEOVERRIDES")
DEFAULT_PORT: Final[int] = 8099
DEFAULT_APPROVER: Final[str] = "dana.finance"
UI_BASE: Final[str] = "http://127.0.0.1:8233"
AGENTCORE_URL: Final[str] = "http://127.0.0.1:8080"
PAYMENTS_URL: Final[str] = "http://127.0.0.1:8081"
EMULATOR_PORT: Final[int] = 4599
DEPLOYMENT_NAME: Final[str] = "support-supervisor"
AGENTS_NS: Final[str] = "agents"
SERVICES_NS: Final[str] = "agent-services"
DEV_TQ: Final[str] = "support-dev-tq"
VERSIONED_TQ: Final[str] = "support-tq"
FINANCE_TQ: Final[str] = "agent-services-tq"
SMOKE_TQ: Final[str] = "nexus-smoke-tq"
ENDPOINT: Final[str] = "finance-agent"


def repo_root(start: Path | None = None) -> Path:
    """``git rev-parse --show-toplevel`` (every script does the same), else the cwd."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start or Path.cwd(),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return Path(out)
    except (OSError, subprocess.CalledProcessError):
        return (start or Path.cwd()).resolve()


def debounce_for(profile: str, env: Mapping[str, str]) -> float:
    """AgentCore debounce: ``AGENTCORE_DEBOUNCE_SECONDS`` (default 20)."""
    raw = env.get("AGENTCORE_DEBOUNCE_SECONDS", "").strip()
    try:
        return float(raw) if raw else 20.0
    except ValueError:
        return 20.0


@dataclass
class Settings:
    """Everything the console needs to know about where it runs."""

    root: Path
    profile: str = "local"
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    ui_base: str = UI_BASE
    agentcore_url: str = AGENTCORE_URL
    payments_url: str = PAYMENTS_URL
    emulator_port: int = EMULATOR_PORT
    #: The environment the console started with (``os.environ`` unless a test passes one).
    base_env: dict[str, str] = field(default_factory=lambda: dict(os.environ))

    @classmethod
    def from_env(
        cls,
        *,
        root: Path | None = None,
        profile: str | None = None,
        port: int | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Settings:
        env = dict(os.environ if env is None else env)
        chosen = profile or env.get("TEMPORAL_PROFILE", "").strip() or "local"
        if chosen not in PROFILES:
            raise ValueError(f"unknown profile {chosen!r}; expected one of {PROFILES}")
        return cls(
            root=(root or repo_root()).resolve(),
            profile=chosen,
            port=port or int(env.get("CONSOLE_PORT", DEFAULT_PORT)),
            agentcore_url=env.get("AGENTCORE_URL", AGENTCORE_URL),
            base_env=env,
        )

    # --- paths -------------------------------------------------------------------------

    @property
    def config_file(self) -> Path:
        raw = self.base_env.get("TEMPORAL_CONFIG_FILE", "").strip()
        path = Path(raw) if raw else self.root / "config" / "temporal.toml"
        return path if path.is_absolute() else (self.root / path).resolve()

    @property
    def temporal_dir(self) -> Path:
        return self.root / ".temporal"

    @property
    def state_dir(self) -> Path:
        return self.temporal_dir / "console"

    @property
    def debounce_s(self) -> float:
        return debounce_for(self.profile, self.base_env)

    @property
    def tls(self) -> bool:
        return self.profile == "kind"

    # --- environments ------------------------------------------------------------------

    def temporal_env(self) -> dict[str, str]:
        """Env for envconfig (the console's own clients): profile env, TLS vars stripped."""
        env = dict(self.base_env)
        env["TEMPORAL_PROFILE"] = self.profile
        env["TEMPORAL_CONFIG_FILE"] = str(self.config_file)
        env.pop("TEMPORAL_NAMESPACE", None)
        if self.profile != "local":
            for name in LOCAL_ONLY_ENV:
                env.pop(name, None)
        return env

    def child_env(
        self, build_id: str | None = None, extra: Mapping[str, str] | None = None
    ) -> dict[str, str]:
        """Env for every spawned ``make`` / script (DESIGN.md 4.5)."""
        env = self.temporal_env()
        # Under `make console` the parent make's flags and command-line variables (e.g.
        # `make console TEMPORAL_PROFILE=kind`) would reach every child make through
        # MAKEFLAGS and override the profile the console switched to.
        for name in MAKE_ENV:
            env.pop(name, None)
        if self.profile == "local":
            env.setdefault("TEMPORAL_TLS", "false")
        if build_id:
            env["TEMPORAL_BUILD_ID"] = build_id
        env.update(extra or {})
        return env

    def config_body(self) -> dict[str, object]:
        """``GET /api/config`` (DESIGN.md 5.1)."""
        from finance_a2a_api import AUTO_APPROVE_LIMIT_USD

        return {
            "profile": self.profile,
            "profiles": list(PROFILES),
            "ui_base": self.ui_base,
            "console_version": __version__,
            "debounce_s": self.debounce_s,
            "auto_approve_limit_usd": AUTO_APPROVE_LIMIT_USD,
            "default_approver": DEFAULT_APPROVER,
        }
