#!/usr/bin/env bash
# Register Support's Worker Deployment Version and make it current (runbook 7.1/7.2).
#   scripts/register-version.sh [--build-id ID] MODE           (make register-version MODE=...)
# MODE:
#   --poller          no compute config: the version appears when a Worker of this Build ID
#                     polls (make agentcore-local + make invoke-local, or
#                     make worker-support-versioned); then set-current-version. v0.7 "I am the WCI".
#   --local-wci       compute config pointing at the AgentCore API emulator
#                     (scripts/agentcore_emulator.py; make dev-server WCI=1): the dev server's
#                     own WCI invokes the local runtime. v0.7, no AWS.
# Steps: worker deployment create (if absent) -> create-version (compute modes) ->
# set-current-version WITHOUT --allow-no-pollers first; if refused, prints the refusal and
# retries with --allow-no-pollers (Q13 evidence). Every step is echoed.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
export TEMPORAL_CONFIG_FILE="${TEMPORAL_CONFIG_FILE:-$PWD/config/temporal.toml}"
export TEMPORAL_PROFILE="${TEMPORAL_PROFILE:-local}"
NS=agents
DEPLOYMENT="${TEMPORAL_DEPLOYMENT_NAME:-support-supervisor}"
BUILD_ID="${TEMPORAL_BUILD_ID:-}"
MODE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --build-id) BUILD_ID="$2"; shift 2 ;;
    --poller|--local-wci) MODE="$1"; shift ;;
    *) echo "usage: $0 [--build-id ID] --poller|--local-wci" >&2; exit 2 ;;
  esac
done
[[ -n "$MODE" ]] || { echo "pick a MODE: --poller or --local-wci" >&2; exit 2; }
BUILD_ID="${BUILD_ID:-$(scripts/build-id.sh)}"
# Echo each command (ARNs masked by scripts/redact.sh), then run it.
run() { echo "+ $*" | scripts/redact.sh; "$@"; }

# A local endpoint ARN for the emulator: placeholder account, runtime support_supervisor_local,
# endpoint b_<build7> (ADR-0004 naming). Built at run time so no 12-digit literal is committed.
local_endpoint_arn() {
  printf 'arn:aws:bedrock-agentcore:%s:%s:runtime/support_supervisor_local/runtime-endpoint/b_%s' \
    "${AWS_REGION:-us-east-1}" "$(printf '0%.0s' {1..12})" "${BUILD_ID:0:7}"
}

# A version that already exists (a Worker polled it) gets its compute config added in place
# (runbook 7.2: update-version-compute-config); otherwise create-version creates it.
version_verb() {
  if temporal worker deployment describe-version --namespace "$NS" --deployment-name "$DEPLOYMENT" \
       --build-id "$BUILD_ID" >/dev/null 2>&1; then
    echo update-version-compute-config
  else
    echo create-version
  fi
}

if temporal worker deployment describe --namespace "$NS" --name "$DEPLOYMENT" >/dev/null 2>&1; then
  echo "deployment $DEPLOYMENT exists"
else
  run temporal worker deployment create --namespace "$NS" --name "$DEPLOYMENT"
fi

case "$MODE" in
  --poller)
    echo "waiting for a Worker of $DEPLOYMENT:$BUILD_ID to poll (make invoke-local / worker-support-versioned)"
    for _ in $(seq 1 120); do
      temporal worker deployment describe-version --namespace "$NS" --deployment-name "$DEPLOYMENT" \
        --build-id "$BUILD_ID" >/dev/null 2>&1 && break
      sleep 0.5
    done
    ;;
  --local-wci)
    run temporal worker deployment "$(version_verb)" --namespace "$NS" --deployment-name "$DEPLOYMENT" \
      --build-id "$BUILD_ID" --aws-agentcore-endpoint-arn "$(local_endpoint_arn)" \
      --aws-agentcore-skip-role-and-external-id
    ;;
esac

echo "+ temporal worker deployment set-current-version --namespace $NS --deployment-name $DEPLOYMENT --build-id $BUILD_ID --yes"
if out="$(temporal worker deployment set-current-version --namespace "$NS" --deployment-name "$DEPLOYMENT" \
            --build-id "$BUILD_ID" --yes 2>&1)"; then
  echo "$out"
  echo "register-version: $DEPLOYMENT:$BUILD_ID is current (no --allow-no-pollers needed)"
else
  echo "$out"
  echo "register-version: refused without --allow-no-pollers; retrying with it (Q13)"
  run temporal worker deployment set-current-version --namespace "$NS" --deployment-name "$DEPLOYMENT" \
    --build-id "$BUILD_ID" --yes --allow-no-pollers
  echo "register-version: $DEPLOYMENT:$BUILD_ID is current (--allow-no-pollers)"
fi
