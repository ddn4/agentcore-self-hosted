#!/usr/bin/env bash
# Observable acceptance checks for v0.1-scaffold (tag v0.1).
# Output is piped through scripts/redact.sh so it can be pasted into the story doc.
#   VERIFY_SKIP_BEDROCK=1  skip the live Opus 5.5 ping (e.g. no AWS session)
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
exec > >(scripts/redact.sh) 2>&1

step() { printf '\n=== %s\n' "$*"; }
started_server=0
cleanup() { [[ $started_server -eq 1 ]] && scripts/dev-server.sh stop || true; }
trap cleanup EXIT

step "1. doctor"
make -s doctor

step "2. temporalio >= 1.34.0 and the plugin import path"
uv tree --frozen --package support-agent --depth 1 | grep -E 'temporalio|strands'
uv run --frozen python -c 'import temporalio.strands_agents as s; print("import temporalio.strands_agents ->", s.StrandsPlugin.__module__)'

step "3. make check (lint, types, unit, workflow, integration, infra-static, scrub-check)"
mkdir -p .temporal
log=.temporal/verify-check.log
make -s check >"$log" 2>&1 || { cat "$log"; exit 1; }
grep -E "passed|All checks|0 errors|infra-static|scrub-check|make check" "$log"

step "4. two-namespace dev server"
if scripts/dev-server.sh status >/dev/null 2>&1; then
  echo "dev server already running; using it"
else
  scripts/dev-server.sh start
  started_server=1
fi
# The CLI reads the same envconfig profile as the SDK (TEMPORAL_CONFIG_FILE + TEMPORAL_PROFILE).
export TEMPORAL_CONFIG_FILE="$PWD/config/temporal.toml" TEMPORAL_PROFILE=local
env -u TEMPORAL_ADDRESS -u TEMPORAL_TLS temporal operator namespace list -o json \
  | jq -r '.[].namespaceInfo.name' | sort
for ns in agents agent-services; do
  env -u TEMPORAL_ADDRESS -u TEMPORAL_TLS temporal operator namespace describe --namespace "$ns" -o json \
    | jq -r '"namespace \(.namespaceInfo.name): \(.namespaceInfo.state)"'
done

step "5. HelloWorkflow per service via envconfig (profile local)"
uv run --frozen python scripts/hello_smoke.py

if [[ "${VERIFY_SKIP_BEDROCK:-0}" == "1" ]]; then
  step "6. bedrock-ping: SKIPPED (VERIFY_SKIP_BEDROCK=1)"
else
  step "6. bedrock-ping: Opus 5.5 via global. and us., no inference params on the wire"
  uv run --frozen python scripts/bedrock_ping.py
fi

step "7. scrub-check (tree and history)"
scripts/scrub-check.sh
scripts/scrub-check.sh --history

step "v0.1-scaffold: VERIFY OK"
