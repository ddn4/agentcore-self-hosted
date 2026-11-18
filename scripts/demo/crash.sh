#!/usr/bin/env bash
# Crash demo (v0.2 story): SIGKILL the local Support Worker while a model turn is in flight.
#   scripts/demo/crash.sh [workflow-id]          (make crash [WF=support-1042])
# 1. wait until the conversation has an invoke_model Activity in STARTED state for the agent
#    CRASH_AT picks: sub-agent (default: Order Lookup or Return Policy agent), supervisor, any.
#    The agent is the Activity's summary in history (what history_report.py prints in [..]).
# 2. kill -9 every local Support Worker process (make worker-support or
#    worker-support-scripted; `uv run` starts python as a child, so both are matched)
# 3. tell the presenter to start the Worker again: the server re-dispatches the turn after
#    the 10 s heartbeat timeout and `make case-a` still prints the reply
# Needs: the Support Worker in its own terminal (not `make dev`: honcho stops every process
# when one exits) and `make case-a` (or case-b) running for the same WF.
# CRASH_WAIT_S (default 120) is how long to wait for a matching model turn.
# Honours TEMPORAL_PROFILE / TEMPORAL_CONFIG_FILE (envconfig).
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
export TEMPORAL_CONFIG_FILE="${TEMPORAL_CONFIG_FILE:-$PWD/config/temporal.toml}"
export TEMPORAL_PROFILE="${TEMPORAL_PROFILE:-local}"
WF="${1:-support-1042}"
WAIT_S="${CRASH_WAIT_S:-120}"
AT="${CRASH_AT:-sub-agent}"
case "$AT" in sub-agent|supervisor|any) ;; *) echo "CRASH_AT must be sub-agent, supervisor or any, got '$AT'" >&2; exit 2 ;; esac
SUPERVISOR="Support Supervisor"
# Ends in a space or end of line, so support_agent.worker_versioned / smoke_worker don't match.
PATTERN='(support_agent\.worker|scripted_worker\.py)( |$)'

pids="$(pgrep -f "$PATTERN" || true)"
if [[ -z "$pids" ]]; then
  echo "no local Support Worker running: start make worker-support (or worker-support-scripted)" >&2
  exit 1
fi

# Prints the agent whose model turn is running now (if it matches CRASH_AT); fails otherwise.
in_flight() {
  local started
  started="$(temporal workflow describe --namespace agents --workflow-id "$WF" -o json 2>/dev/null \
    | jq -c '[.pendingActivities[]? | select(.activityType.name=="invoke_model"
              and .state=="PENDING_ACTIVITY_STATE_STARTED") | .activityId]')" || return 1
  [[ "$started" != "[]" && -n "$started" ]] || return 1
  temporal workflow show --namespace agents --workflow-id "$WF" -o json 2>/dev/null \
    | jq -er --argjson ids "$started" --arg at "$AT" --arg sup "$SUPERVISOR" '
        [.events[] | select(.activityTaskScheduledEventAttributes)
         | select(.activityTaskScheduledEventAttributes.activityId as $i | $ids | any(. == $i))
         | (.userMetadata.summary.data // "" | if . == "" then "" else @base64d | fromjson end)
         | select($at == "any" or ($at == "supervisor" and . == $sup)
                  or ($at == "sub-agent" and . != "" and . != $sup))]
        | first // empty'
}

echo "waiting for a $AT model turn (invoke_model) on $WF (up to ${WAIT_S} s)..."
deadline=$((SECONDS + WAIT_S))
until agent="$(in_flight)"; do
  if (( SECONDS >= deadline )); then
    echo "timed out: no $AT invoke_model in flight on $WF (is make case-a WF=$WF running?)" >&2
    exit 1
  fi
  sleep 0.3
done

# Re-read the PIDs: the Worker may have been restarted while we waited.
pids="$(pgrep -f "$PATTERN" || true)"
if [[ -z "$pids" ]]; then
  echo "the Support Worker exited while we waited; nothing to kill" >&2
  exit 1
fi
# shellcheck disable=SC2086
kill -9 $pids
echo "SIGKILL sent to the Support Worker (pid $(echo $pids | tr ' ' ',')) during invoke_model [${agent:-?}] on $WF"
echo "start it again (make worker-support); the turn is re-dispatched after the 10 s heartbeat timeout"
