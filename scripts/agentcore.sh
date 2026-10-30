#!/usr/bin/env bash
# The pinned AgentCore CLI (make tools-agentcore -> .tools/agentcore-cli, @aws/agentcore 1.0.1).
#   scripts/agentcore.sh <args...>      e.g. scripts/agentcore.sh --version
# Telemetry is off, and `agentcore deploy` must not switch on CloudWatch Transaction Search
# account-wide (a shared account): `transactionSearch` is set to false before any call.
# The CLI keeps that global config in ~/.agentcore/config.json (fixed path in 1.0.1;
# AGENTCORE_CONFIG_DIR only moves its keychain file, which we keep in .tools/).
set -euo pipefail
root="$(git rev-parse --show-toplevel)"
bin="$root/.tools/agentcore-cli/node_modules/.bin/agentcore"
[[ -x "$bin" ]] || { echo "agentcore: run 'make tools-agentcore' first" >&2; exit 1; }
export AGENTCORE_TELEMETRY_DISABLED=1 AGENTCORE_CONFIG_DIR="$root/.tools/agentcore-config"
export AWS_REGION="${AWS_REGION:-us-east-1}"
mkdir -p "$AGENTCORE_CONFIG_DIR"
if [[ "$("$bin" config transactionSearch 2>/dev/null | tail -1)" != false ]]; then
  "$bin" config transactionSearch false >/dev/null
  "$bin" config telemetry.enabled false >/dev/null 2>&1 || true
fi
exec "$bin" "$@"
