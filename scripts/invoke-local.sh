#!/usr/bin/env bash
# "I am the WCI": invoke the local AgentCore-shaped Worker by hand (plan v0.7).
#   scripts/invoke-local.sh            (make invoke-local)
# Sends what the WCI sends (runbook 1.2 step 3, and observed from the dev server's WCI via
# scripts/agentcore_emulator.py): POST /invocations with {deploymentName, buildId} and a
# session ID header, then shows /ping. The Build ID is TEMPORAL_BUILD_ID or scripts/build-id.sh.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
URL="${AGENTCORE_URL:-http://127.0.0.1:8080}"
BUILD_ID="${TEMPORAL_BUILD_ID:-$(scripts/build-id.sh)}"
DEPLOYMENT="${TEMPORAL_DEPLOYMENT_NAME:-support-supervisor}"
SESSION="local-$(date +%s)-$$"
payload="$(jq -cn --arg d "$DEPLOYMENT" --arg b "$BUILD_ID" '{deploymentName: $d, buildId: $b}')"
echo "POST $URL/invocations $payload"
curl -sS -X POST "$URL/invocations" -H 'content-type: application/json' \
  -H "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: $SESSION" -d "$payload"
echo
echo "GET  $URL/ping -> $(curl -sS "$URL/ping")"
