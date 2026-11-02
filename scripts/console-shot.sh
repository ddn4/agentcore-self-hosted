#!/usr/bin/env bash
# Headless Chrome screenshot of the demo console (docs/console/DESIGN.md 6.3).
#   scripts/console-shot.sh <url-path> <out.png> [width] [height] [--full] [--click SEL] [--wait S]
#       a page of the running console (CONSOLE_URL, default http://127.0.0.1:8099)
#   scripts/console-shot.sh --all     (make console-shots)
#       every fixture at 1440x900 and 390x844, dark and light -> .temporal/console/shots/;
#       serves static/ itself, so neither the console nor Temporal needs to run.
# Uses the installed Google Chrome (CHROME=...) over the DevTools protocol with a throwaway
# profile; Chrome exits when the screenshots are written.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
exec uv run --frozen python scripts/console_shot.py "$@"
