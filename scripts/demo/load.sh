#!/usr/bin/env bash
# Small load test against Support's serverless version (plan v0.13; runbook Phase 8 step 6).
#   scripts/demo/load.sh [N]          (make load N=10 [LOAD_KIND=case-a|ping])
# Needs the local serverless shape: make dev-server WCI=1, make agentcore-emulator,
# make agentcore-local (or -scripted), make register-version MODE=--local-wci, plus Finance.
# Starts N Workflows at once on the versioned queue support-tq (default 10), with only the
# AgentCore-shaped Worker able to poll it, and waits for all of them.
#   LOAD_KIND=case-a (default)  N x Case A (#1042, $40, auto-approved): real model turns
#                               (Bedrock tokens: ~N x 10 calls) unless the Workers are scripted
#   LOAD_KIND=ping              N x PingWorkflow (one Activity, no model)
# Success criteria: all N complete and no failed WCI Activity in the window.
# Prints: per-Workflow status and duration and the WCI InvokeWorker count.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
export TEMPORAL_CONFIG_FILE="${TEMPORAL_CONFIG_FILE:-$PWD/config/temporal.toml}"
export TEMPORAL_PROFILE="${TEMPORAL_PROFILE:-local}"
N="${1:-${N:-10}}"
KIND="${LOAD_KIND:-case-a}"
TQ="${LOAD_TASK_QUEUE:-support-tq}"
PREFIX="load-$(date +%s)"
OUT="${LOAD_OUT:-.temporal/load/$PREFIX}"
mkdir -p "$OUT"
(( N >= 1 && N <= 20 )) || { echo "N must be 1..20 here (N=50 is v1.2), got $N" >&2; exit 2; }
T0="$(date +%s)"; T0ISO="$(date -u +%FT%TZ)"
echo "load: $N x $KIND on agents/$TQ, prefix $PREFIX, start $T0ISO"

pids=()
for i in $(seq 1 "$N"); do
  wf="$PREFIX-$i"
  case "$KIND" in
    case-a)
      ( s=$(date +%s); SUPPORT_TASK_QUEUE="$TQ" scripts/demo/case-a.sh "$wf" >"$OUT/$i.log" 2>&1; rc=$?
        echo "$wf rc=$rc $(( $(date +%s) - s ))s" >"$OUT/$i.rc" ) & pids+=("$!") ;;
    ping)
      ( s=$(date +%s); temporal workflow execute --namespace agents --task-queue "$TQ" --type PingWorkflow \
          --workflow-id "$wf" --execution-timeout 15m --input "\"$wf\"" >"$OUT/$i.log" 2>&1; rc=$?
        echo "$wf rc=$rc $(( $(date +%s) - s ))s" >"$OUT/$i.rc" ) & pids+=("$!") ;;
    *) echo "LOAD_KIND must be case-a or ping, got '$KIND'" >&2; exit 2 ;;
  esac
done
wait "${pids[@]}" || true
WALL=$(( $(date +%s) - T0 ))

completed=0
for i in $(seq 1 "$N"); do
  st="$(temporal workflow describe --namespace agents --workflow-id "$PREFIX-$i" -o json \
        | jq -r '.workflowExecutionInfo.status | sub("WORKFLOW_EXECUTION_STATUS_"; "")')"
  echo "  $(cat "$OUT/$i.rc")  $st"
  [[ "$st" == COMPLETED ]] && completed=$((completed + 1))
done
echo "load: $completed/$N completed in ${WALL}s wall clock"

BUILD="$(temporal worker deployment describe --namespace agents --name support-supervisor -o json \
          | jq -r .routingConfig.currentVersionBuildID)"
scripts/wci-history.sh "$BUILD" --since "$T0ISO" >"$OUT/wci.log" 2>&1 || true
inv="$(grep -c 'activity  InvokeWorker$' "$OUT/wci.log" || true)"
failed="$(grep -c '  failed  ' "$OUT/wci.log" || true)"
echo "load: WCI InvokeWorker during the load: $inv; failed WCI activities: $failed"
[[ "$completed" == "$N" && "$failed" == 0 ]]
