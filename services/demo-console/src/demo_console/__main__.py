"""``python -m demo_console [--host 127.0.0.1] [--port 8099] [--profile P]``   (make console)

The demo console on http://127.0.0.1:8099 (docs/console/DESIGN.md). Runs from the repo root
(cert paths in config/temporal.toml are relative to it). TEMPORAL_PROFILE picks the profile
(local | kind | kind-plaintext | eks-pf); CONSOLE_ALLOW_AWS=1 enables AWS-backed actions.
"""

from __future__ import annotations

import argparse
import os

import uvicorn

from demo_console.app import create_app
from demo_console.settings import DEFAULT_PORT, PROFILES, Settings, repo_root


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Demo console (docs/console/DESIGN.md)")
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1"])
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("CONSOLE_PORT", DEFAULT_PORT))
    )
    parser.add_argument("--profile", choices=PROFILES, default=None)
    args = parser.parse_args(argv)
    root = repo_root()
    os.chdir(root)
    settings = Settings.from_env(root=root, profile=args.profile, port=args.port)
    app = create_app(settings)
    print(
        f"demo console: http://127.0.0.1:{args.port}/  (profile {settings.profile}"
        f"{', AWS actions allowed' if settings.allow_aws else ''})",
        flush=True,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
