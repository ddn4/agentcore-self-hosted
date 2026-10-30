#!/usr/bin/env bash
# Packaging check for the CodeZip bundle (plan v0.7, section 5 "Packaging").
#   scripts/agentcore-bundle-test.sh       (make agentcore-bundle-test; run make agentcore-bundle first)
# 1. Fresh Python 3.12 venv, install the bundle's pyproject.toml pins only (no workspace;
#    exactly what AgentCore's CodeZip packaging installs, ledger 22), then from the
#    bundle directory: import the entrypoint, the Workflows and the contract.
# 2. Serve the entrypoint from that venv: /ping Healthy; /invocations acks at once; with an
#    unreachable (blackholed) Temporal address the Worker gives up after TEMPORAL_CONNECT_TIMEOUT_SECONDS and /ping
#    returns to Healthy (complete_async_task ran).
# 3. Size: install the same pins for AgentCore's platform (linux arm64, CPython 3.12,
#    wheels only), zip code + dependencies, and check it is under 250 MB.
# Needs network access to PyPI (pip only; no AWS).
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
B=build/agentcore
[[ -f "$B/agentcore_worker.py" ]] || { echo "run make agentcore-bundle first" >&2; exit 1; }
VENV=build/agentcore-venv
DEPS=build/agentcore-arm64
LIMIT_MB=250
PORT="${BUNDLE_TEST_PORT:-18080}"

echo "--- 1. fresh 3.12 venv + pyproject.toml dependencies"
rm -rf "$VENV"
uv venv --quiet --python 3.12 "$VENV"
uv pip install --quiet --python "$VENV/bin/python" -r "$B/pyproject.toml"
PY="$PWD/$VENV/bin/python"
"$PY" -c 'import sys; print("python", sys.version.split()[0], "| venv", sys.prefix)'
(cd "$B" && exec env -i PATH="$PATH" HOME="$HOME" "$PY" - <<'PY'
import importlib, sys
assert sys.path[0] == "", sys.path[:2]     # run from the code location, like AgentCore
for name in ("agentcore_worker", "support_agent.agentcore_app", "support_agent.workflows",
             "support_agent.ping", "finance_a2a_api", "temporalio.strands_agents"):
    mod = importlib.import_module(name)
    where = getattr(mod, "__file__", "?")
    print(f"  import {name:30s} ok  ({where.split('site-packages/')[-1] if 'site-packages' in where else where})")
import agentcore_worker, support_agent.config as c
assert "main" in agentcore_worker.app.handlers, "no @app.entrypoint"
print("  entrypoint registered; BUILD_ID file ->", c.build_id())
PY
)

echo "--- 2. serve the entrypoint from the fresh venv (Temporal address unreachable: a blackhole, so the connect timeout fires)"
log=build/agentcore-bundle-test.log
(cd "$B" && exec env -i PATH="$PATH" HOME="$HOME" AGENTCORE_PORT="$PORT" \
   TEMPORAL_ADDRESS="${BUNDLE_TEST_BLACKHOLE:-10.255.255.1:7233}" TEMPORAL_CONNECT_TIMEOUT_SECONDS=3 TEMPORAL_CONFIG_FILE=/dev/null \
   "$PY" agentcore_worker.py) >"$log" 2>&1 &
pid=$!
trap 'kill $pid 2>/dev/null || true' EXIT
for _ in $(seq 1 60); do curl -sf "http://127.0.0.1:$PORT/ping" >/dev/null && break; sleep 0.25; done
echo "  /ping        $(curl -s "http://127.0.0.1:$PORT/ping")"
t0=$(date +%s)
echo "  /invocations $(curl -s -X POST "http://127.0.0.1:$PORT/invocations" -H 'content-type: application/json' -d '{}')"
busy="$(curl -s "http://127.0.0.1:$PORT/ping")"
echo "  /ping        $busy  (right after the invocation)"
ok=0
for _ in $(seq 1 80); do
  if curl -s "http://127.0.0.1:$PORT/ping" | grep -q '"Healthy"'; then ok=1; break; fi; sleep 0.25
done
echo "  /ping        $(curl -s "http://127.0.0.1:$PORT/ping")  (after $(( $(date +%s) - t0 ))s: the 3 s connect timeout fired)"
grep -aE "could not connect|async task released" "$log" | sed -E 's/^[0-9-]+ [0-9:,]+ //; s/^/  log: /' | head -3
kill $pid 2>/dev/null || true; wait $pid 2>/dev/null || true; trap - EXIT
[[ "$busy" == *HealthyBusy* ]] || { echo "FAIL: /ping was not HealthyBusy while the Worker ran"; exit 1; }
[[ $ok -eq 1 ]] || { echo "FAIL: /ping did not return to Healthy"; cat "$log"; exit 1; }
grep -aq "could not connect to Temporal within 3s" "$log" || { echo "FAIL: no connect timeout in the log"; exit 1; }

echo "--- 3. size for AgentCore (linux arm64, CPython 3.12, wheels only)"
rm -rf "$DEPS"
uv pip install --quiet --target "$DEPS" --python-platform aarch64-manylinux_2_28 \
  --python-version 3.12 --only-binary :all: -r "$B/pyproject.toml"
zipf=build/agentcore-codezip-test.zip; rm -f "$zipf"
(cd "$B" && zip -qr "$OLDPWD/$zipf" . -x '*/__pycache__/*')
(cd "$DEPS" && zip -qr "$OLDPWD/$zipf" . -x '*/__pycache__/*')
bytes=$(wc -c <"$zipf" | tr -d ' ')
mb=$(( (bytes + 1048575) / 1048576 ))
echo "  code only: $(cd "$B" && zip -qr - . | wc -c | awk '{printf "%.1f KB", $1/1024}'); code + arm64 deps zipped: ${mb} MB (limit ${LIMIT_MB} MB)"
echo "  largest dependencies (unzipped):"
du -sk "$DEPS"/* | sort -rn | head -5 | awk '{printf "    %6.1f MB  %s\n", $1/1024, $2}' | sed "s|$DEPS/||"
[[ $mb -lt $LIMIT_MB ]] || { echo "FAIL: bundle is ${mb} MB (>= ${LIMIT_MB} MB)"; exit 1; }
echo "agentcore-bundle-test: OK"
