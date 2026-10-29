#!/usr/bin/env bash
# Observable acceptance checks for v0.3-sub-agents (IMPLEMENTATION_PLAN.md v0.3).
# Output is piped through scripts/redact.sh so it can be pasted into the story doc.
#   VERIFY_SKIP_BEDROCK=1  skip the live Opus 5.5 steps (6-9)
#   RECORD_HISTORY=1       re-record the replay fixtures in services/support-agent/tests/histories
#                          (offline: support-crash-scripted, support-2077-scripted;
#                           live: support-1042, support-2077)
#   VERIFY_SKIP_CHECK=1    skip step 1 (only to bootstrap fixtures that make check itself replays)
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
exec > >(scripts/redact.sh) 2>&1

export TEMPORAL_CONFIG_FILE="$PWD/config/temporal.toml" TEMPORAL_PROFILE=local
export SUPPORT_TASK_QUEUE="${SUPPORT_TASK_QUEUE:-support-dev-tq}"
UV=(uv run --frozen)
# Workers are started with the venv's python directly, not `uv run`: `uv run`
# spawns python as a child, so `kill -9 $!` would hit uv and orphan the Worker.
PY=.venv/bin/python
LOGS=.temporal/verify-v0.3
mkdir -p "$LOGS"
RECORD="${RECORD_HISTORY:-0}"

step() { printf '\n=== %s\n' "$*"; }
started_server=0
pids=()
cleanup() {
  for p in "${pids[@]:-}"; do [[ -n "$p" ]] && kill "$p" 2>/dev/null || true; done
  [[ $started_server -eq 1 ]] && scripts/dev-server.sh stop || true
}
trap cleanup EXIT

record() { # record <workflow-id> <fixture-name>
  [[ "$RECORD" == "1" ]] && "${UV[@]}" python scripts/record_history.py "$1" --name "$2" || true
}

# Wait until lookup_order has completed and an invoke_model Activity is running:
# that is the Order Lookup agent's second model turn, inside the Supervisor's
# order_lookup_agent tool (two agent loops deep).
wait_mid_sub_agent_turn() {
  local wf="$1"
  for _ in $(seq 1 240); do
    if "${UV[@]}" python scripts/history_report.py "$wf" 2>/dev/null | grep -q "lookup_order .*completed" &&
       temporal workflow describe --namespace agents --workflow-id "$wf" -o json \
         | jq -e '[.pendingActivities[]? | select(.activityType.name=="invoke_model"
                   and .state=="PENDING_ACTIVITY_STATE_STARTED")] | length > 0' >/dev/null; then
      return 0
    fi
    sleep 0.5
  done
  echo "timed out waiting for the Order Lookup agent's in-flight invoke_model on $wf" >&2
  return 1
}

# crash_demo <label> <workflow-id> <worker command...>
# Worker A, Case A, kill -9 Worker A inside the Order Lookup agent's model turn,
# Worker B, wait for the reply, print the attributed Activity table.
crash_demo() {
  local label="$1" wf="$2"; shift 2
  "$@" >"$LOGS/$label-worker-a.log" 2>&1 & local a=$!; pids+=("$a")
  sleep 3
  scripts/demo/case-a.sh "$wf" >"$LOGS/$label-case-a.log" 2>&1 & local starter=$!; pids+=("$starter")
  wait_mid_sub_agent_turn "$wf"
  kill -9 "$a"
  echo "worker A (pid $a) killed with SIGKILL during invoke_model [Order Lookup agent]"
  "$@" >"$LOGS/$label-worker-b.log" 2>&1 & local b=$!; pids+=("$b")
  echo "worker B (pid $b) started; the server re-dispatches the turn after the 10 s heartbeat timeout"
  local t0=$SECONDS
  wait "$starter"
  echo "Case A finished $((SECONDS - t0)) s after the kill. Reply:"
  grep -v '^waiting for' "$LOGS/$label-case-a.log" | sed 's/^/  /'
  kill "$b" 2>/dev/null || true; wait "$b" 2>/dev/null || true
  "${UV[@]}" python scripts/history_report.py "$wf" | tee "$LOGS/$label-history.txt"
  grep -Eq "^   4\.    invoke_model +\[Order Lookup agent\] +attempts=2 completed" "$LOGS/$label-history.txt"
  grep -Eq "^   3\.    lookup_order +attempts=1 completed" "$LOGS/$label-history.txt"
  [[ "$(grep -c 'attempts=2' "$LOGS/$label-history.txt")" == "1" ]]
  for agent in "Support Supervisor" "Order Lookup agent" "Return Policy agent"; do
    grep -q "invoke_model .*\[$agent\]" "$LOGS/$label-history.txt"
  done
  echo "$label: three agents attributed; only the interrupted sub-agent turn was retried; lookup_order ran once"
}

# case_b <label> <workflow-id> <worker command...>: Case B (2077) must say "escalat".
case_b() {
  local label="$1" wf="$2"; shift 2
  "$@" >"$LOGS/$label-worker.log" 2>&1 & local w=$!; pids+=("$w")
  sleep 3
  scripts/demo/case-b.sh "$wf" >"$LOGS/$label-case-b.log" 2>&1
  grep -v '^waiting for' "$LOGS/$label-case-b.log" | grep -E 'Status|Result ' | sed 's/^/  /'
  echo "sub_agent_runs query (make sub-agents WF=$wf):"
  # Queries are answered by a Worker, so ask before stopping it.
  temporal workflow query --namespace agents --workflow-id "$wf" --type sub_agent_runs -o json \
    | jq -r '.queryResult[0][] | "  \(.agent): \(.answer)"'
  kill "$w" 2>/dev/null || true; wait "$w" 2>/dev/null || true
  "${UV[@]}" python scripts/history_report.py "$wf"
  grep -qi 'escalat' "$LOGS/$label-case-b.log"
  grep -q '450' "$LOGS/$label-case-b.log"
  echo "$label: the 2077 reply says the request needs escalation"
}

step "1. make check (lint, types, unit, workflow, integration, infra-static, scrub-check)"
if [[ "${VERIFY_SKIP_CHECK:-0}" == "1" ]]; then
  echo "SKIPPED (VERIFY_SKIP_CHECK=1)"
else
  log=$LOGS/check.log
  make -s check >"$log" 2>&1 || { cat "$log"; exit 1; }
  grep -E "passed|All checks|0 errors|infra-static|scrub-check|make check" "$log"
fi

step "2. spike (offline): nested agents as tools; Strands as_tool() and shared-instance probes (Q20)"
"${UV[@]}" python spikes/nested_agents.py >"$LOGS/spike.log" 2>&1 || { cat "$LOGS/spike.log"; exit 1; }
grep -vE 'WARN|^Temporal|^$' "$LOGS/spike.log" | grep -vE '^\s+[0-9]+\. ' | grep -v 'Persistence\|Metrics'

step "3. two-namespace dev server"
if scripts/dev-server.sh status >/dev/null 2>&1; then
  echo "dev server already running; using it"
else
  scripts/dev-server.sh start
  started_server=1
fi
# Earlier tags' runs on the unversioned dev queue would not replay on this code.
open_old=$(temporal workflow list --namespace agents \
  --query "WorkflowType='SupportSupervisorWorkflow' AND ExecutionStatus='Running'" -o json | jq length)
echo "open SupportSupervisorWorkflow runs before starting v0.3 Workers: $open_old"

step "4. crash safety, offline: kill -9 a real Worker mid sub-agent turn (scripted model)"
wf="support-crash-scripted-$(date +%s)"
crash_demo scripted "$wf" "$PY" scripts/scripted_worker.py --delay 3
record "$wf" support-crash-scripted

step "5. Case B, offline: 2077 is escalated (scripted model)"
wf="support-2077-scripted-$(date +%s)"
case_b scripted "$wf" "$PY" scripts/scripted_worker.py --delay 0
record "$wf" support-2077-scripted

if [[ "${VERIFY_SKIP_BEDROCK:-0}" == "1" ]]; then
  step "6-9. live Opus 5.5 steps: SKIPPED (VERIFY_SKIP_BEDROCK=1)"
else
  step "6. carried from v0.2 (ledger 49): wire params, rejections, effort field and latency"
  RUN_BEDROCK_TESTS=1 "${UV[@]}" python spikes/opus_params.py --turns 3

  step "7. carried from v0.2 (ledger 49): structured output vs forced tool_choice"
  RUN_BEDROCK_TESTS=1 "${UV[@]}" python spikes/opus_structured_output.py

  step "8. live Case A on Opus 5.5, kill -9 mid sub-agent turn (make worker-support + make case-a)"
  wf=support-1042
  temporal workflow terminate --namespace agents --workflow-id "$wf" --reason "verify reset" >/dev/null 2>&1 || true
  crash_demo live "$wf" "$PY" -m support_agent.worker
  reply="$(cat "$LOGS/live-case-a.log")"
  grep -q '40' <<<"$reply" && grep -qi 'eligib' <<<"$reply"
  echo "reply mentions \$40 and eligibility"
  grep -h "support worker:" "$LOGS"/live-worker-a.log | sed 's/^.*support worker:/support worker:/'
  record "$wf" support-1042

  step "9. live Case B on Opus 5.5 (make case-b)"
  wf=support-2077
  temporal workflow terminate --namespace agents --workflow-id "$wf" --reason "verify reset" >/dev/null 2>&1 || true
  case_b live "$wf" "$PY" -m support_agent.worker
  record "$wf" support-2077
fi

if [[ "$RECORD" == "1" ]]; then
  step "re-recorded fixtures replay on today's code"
  "${UV[@]}" pytest -q services/support-agent/tests/test_replay.py
fi

step "10. scrub-check (tree and history)"
scripts/scrub-check.sh
scripts/scrub-check.sh --history

step "v0.3-sub-agents: VERIFY OK"
