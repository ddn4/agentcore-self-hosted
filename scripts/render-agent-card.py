"""Render the Finance Agent Card as markdown (the Nexus endpoint description).

    uv run python scripts/render-agent-card.py [--out .generated/finance-agent-card.md]

Writes ``finance_a2a_api.render_markdown()``; ``scripts/nexus-endpoint.sh`` passes
the file to ``temporal operator nexus endpoint create|update --description-file``
(runbook 7b.8). ``packages/finance-a2a-api/tests/test_agent_card.py`` keeps the
committed golden copy in sync with the code.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from finance_a2a_api import render_markdown


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path(".generated/finance-agent-card.md"))
    args = parser.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_markdown(), encoding="utf-8")
    print(f"agent card -> {args.out}")


if __name__ == "__main__":
    main()
