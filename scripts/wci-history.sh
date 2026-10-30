#!/usr/bin/env bash
# The WCI's own history for Support's Worker Deployment Version (plan v0.7/v0.8, Q6).
#   scripts/wci-history.sh [BUILD_ID] [--since ISO8601]     (make wci-history BUILD=...)
# Found at v0.7 on the dev server (server 1.32.0; ledger 25): the WCI is a system Workflow
#   type  temporal-sys-worker-controller-instance-workflow
#   id    temporal-sys-worker-controller-instance:<deployment>:<build id>
#   in the deployment's own namespace (agents), task queue temporal-sys-per-ns-tq.
# It is not listed by visibility queries but `workflow show` works by ID. Each invocation is
# an InvokeWorker Activity after a `task-add-signal` from matching, and the run then
# continues-as-new, so this walks the continue-as-new chain backwards and prints one line per
# signal / update / Activity, oldest first. Output goes through scripts/redact.sh.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
export TEMPORAL_CONFIG_FILE="${TEMPORAL_CONFIG_FILE:-$PWD/config/temporal.toml}"
export TEMPORAL_PROFILE="${TEMPORAL_PROFILE:-local}"
BUILD_ID="${1:-${TEMPORAL_BUILD_ID:-$(scripts/build-id.sh)}}"
SINCE="${3:-}"; [[ "${2:-}" == "--since" ]] || SINCE=""
WF="temporal-sys-worker-controller-instance:${TEMPORAL_DEPLOYMENT_NAME:-support-supervisor}:$BUILD_ID"
MAX_RUNS="${WCI_HISTORY_MAX_RUNS:-200}"

run="" lines=() n=0
while :; do
  args=(--namespace agents --workflow-id "$WF" -o json)
  [[ -n "$run" ]] && args+=(--run-id "$run")
  json="$(temporal workflow show "${args[@]}" 2>/dev/null)" || { [[ $n -eq 0 ]] && { echo "no WCI Workflow $WF (no compute config, or WCI off)" >&2; exit 1; }; break; }
  chunk="$(jq -r --arg since "$SINCE" '.events[]
    | select($since == "" or .eventTime >= $since)
    | . as $e | (if .eventType == "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED" then "activity  " + .activityTaskScheduledEventAttributes.activityType.name
         elif .eventType == "EVENT_TYPE_ACTIVITY_TASK_FAILED" then "  failed  " + (.activityTaskFailedEventAttributes.failure.message // "" | .[0:120])
         elif .eventType == "EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED" then "signal    " + .workflowExecutionSignaledEventAttributes.signalName
         elif .eventType == "EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED" then "update    " + .workflowExecutionUpdateAcceptedEventAttributes.acceptedRequest.input.name
         elif .eventType == "EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW" then "continue-as-new"
         else empty end) as $what | [$e.eventTime, $what] | @tsv' <<<"$json")"
  [[ -n "$chunk" ]] && lines=("$chunk" ${lines[@]+"${lines[@]}"})
  run="$(jq -r '.events[0].workflowExecutionStartedEventAttributes.continuedExecutionRunId // empty' <<<"$json")"
  n=$((n + 1))
  [[ -z "$run" || $n -ge $MAX_RUNS ]] && break
done
echo "WCI $WF (namespace agents, $n runs)"
printf '%s\n' ${lines[@]+"${lines[@]}"} | sed 's/^/  /'
invokes=$(printf '%s\n' ${lines[@]+"${lines[@]}"} | grep -c "activity  InvokeWorker$" || true)
echo "InvokeWorker activities: $invokes"
