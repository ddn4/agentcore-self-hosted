#!/usr/bin/env bash
# Headless screenshot of a Temporal Web UI page (v0.13: Agent Card, Q9 compute config).
#   scripts/ui-screenshot.sh <path> <out.png> [height]     (make ui-shot PAGE=/nexus OUT=...)
# The UI must be reachable at UI_URL (default http://127.0.0.1:8233: make pf). Uses the
# installed Google Chrome (CHROME=...) in headless mode with a throwaway profile under
# .temporal/chrome-profile; Chrome exits when the screenshot is written. Screenshots stay in
# .temporal/ (gitignored): the story doc describes them.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
PAGE="${1:?page path, e.g. /nexus}"; OUT="${2:?output png}"; H="${3:-1400}"
UI="${UI_URL:-http://127.0.0.1:8233}"
CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
[[ -x "$CHROME" ]] || { echo "ui-screenshot: no Chrome at $CHROME (set CHROME=)" >&2; exit 2; }
curl -sf "$UI/api/v1/settings" >/dev/null || { echo "ui-screenshot: UI not reachable at $UI (make pf)" >&2; exit 1; }
mkdir -p "$(dirname "$OUT")" .temporal/chrome-profile
"$CHROME" --headless=new --disable-gpu --no-first-run --user-data-dir="$PWD/.temporal/chrome-profile" \
  --window-size=1400,"$H" --virtual-time-budget=20000 --screenshot="$OUT" "$UI$PAGE" >/dev/null 2>&1
[[ -s "$OUT" ]] && echo "ui-screenshot: $UI$PAGE -> $OUT"
