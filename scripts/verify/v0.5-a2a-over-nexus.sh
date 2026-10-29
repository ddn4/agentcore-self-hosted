#!/usr/bin/env bash
# Observable acceptance checks for v0.5-a2a-over-nexus (IMPLEMENTATION_PLAN.md v0.5).
# Output is piped through scripts/redact.sh so it can be pasted into the story doc.
#   VERIFY_SKIP_BEDROCK=1  skip the live Opus 5.5 steps (9-10); everything else is offline
#   RECORD_HISTORY=1       re-record the replay fixtures in services/support-agent/tests/histories
#                          (offline: support-crash-scripted, support-1042-scripted,
#                           support-1042-handoff-scripted, support-2077-scripted;
#                           live: support-1042, support-2077)
#   VERIFY_SKIP_CHECK=1    skip step 1 (only to bootstrap fixtures that make check itself replays)
# Starts its own dev server (unless one is running), payments-mock, Finance and Support Workers,
# and stops everything it started on exit.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
exec > >(scripts/redact.sh) 2>&1

export TEMPORAL_CONFIG_FILE="$PWD/config/temporal.toml" TEMPORAL_PROFILE=local
export SUPPORT_TASK_QUEUE="${SUPPORT_TASK_QUEUE:-support-dev-tq}"
export PAYMENTS_URL=http://127.0.0.1:8081
UV=(uv run --frozen)
# Workers are started with the venv's python directly, not `uv run`: `uv run`
# spawns python as a child, so `kill -9 $!` would hit uv and orphan the Worker.
PY=.venv/bin/python
LOGS=.temporal/verify-v0.5
rm -rf "$LOGS"; mkdir -p "$LOGS"
RECORD="${RECORD_HISTORY:-0}"
TS=$(date +%s)

step() { printf '\n=== %s\n' "$*"; }
started_server=0
pids=()
support_pid=""
finance_pid=""
cleanup() {
  for p in "${pids[@]:-}"; do [[ -n "$p" ]] && kill "$p" 2>/dev/null || true; done
  for p in "${pids[@]:-}"; do [[ -n "$p" ]] && wait "$p" 2>/dev/null || true; done
  [[ $started_server -eq 1 ]] && scripts/dev-server.sh stop || true
}
trap cleanup EXIT

record() { # record <workflow-id> <fixture-name>
  [[ "$RECORD" == "1" ]] && "${UV[@]}" python scripts/record_history.py "$1" --name "$2" || true
}
start_support() { # start_support <label> <command...>
  local label="$1"; shift
  stop_support
  "$@" >"$LOGS/support-$label.log" 2>&1 & support_pid=$!; pids+=("$support_pid")
  sleep 3
}
stop_support() {
  [[ -n "$support_pid" ]] && { kill "$support_pid" 2>/dev/null || true; wait "$support_pid" 2>/dev/null || true; }
  support_pid=""
}
start_finance() { # start_finance <label> <command...>
  local label="$1"; shift
  [[ -n "$finance_pid" ]] && { kill "$finance_pid" 2>/dev/null || true; wait "$finance_pid" 2>/dev/null || true; }
  "$@" >"$LOGS/finance-$label.log" 2>&1 & finance_pid=$!; pids+=("$finance_pid")
  sleep 3
}
# run_case <case-a|case-b> <workflow-id> <mode> <expected A2A state>
run_case() {
  local case="$1" wf="$2" mode="$3" state="$4"
  temporal workflow terminate --namespace agents --workflow-id "$wf" --reason "verify reset" >/dev/null 2>&1 || true
  SUPPORT_A2A_MODE="$mode" scripts/demo/"$case".sh "$wf" >"$LOGS/$wf.log" 2>&1
  grep -E '^ *Result' -A1 "$LOGS/$wf.log" | sed 's/^/  /' || cat "$LOGS/$wf.log"
  "${UV[@]}" python scripts/check_a2a_run.py "$wf" "$state" --mode "$mode"
}

step "1. make check (lint, types, unit, workflow, integration, infra-static, scrub-check)"
if [[ "${VERIFY_SKIP_CHECK:-0}" == "1" ]]; then
  echo "SKIPPED (VERIFY_SKIP_CHECK=1)"
else
  make -s check >"$LOGS/check.log" 2>&1 || { cat "$LOGS/check.log"; exit 1; }
  grep -E "passed|0 errors|infra-static|scrub-check|make check" "$LOGS/check.log"
fi

step "2. Q19a offline: the plugin awaits an async in-workflow @tool that awaits a Nexus operation"
"${UV[@]}" pytest -q -p no:randomly services/support-agent/tests/test_workflow.py \
  services/support-agent/tests/test_crash_safety.py tests/test_supervisor_a2a.py -rA 2>&1 \
  | grep -E '^PASSED' | sed -E 's/^PASSED [^:]*::/  /' | grep -E 'mode|drift|finance|crash|worker_dies|case_' || true
"${UV[@]}" pytest -q services/support-agent/tests/test_workflow.py \
  services/support-agent/tests/test_crash_safety.py tests/test_supervisor_a2a.py 2>&1 | tail -1

step "3. dev server (both namespaces); runs from older Workers on the dev queue"
if scripts/dev-server.sh status >/dev/null 2>&1; then
  echo "dev server already running; using it"
else
  scripts/dev-server.sh start
  started_server=1
fi
open_old=$(temporal workflow list --namespace agents \
  --query "WorkflowType='SupportSupervisorWorkflow' AND ExecutionStatus='Running'" -o json | jq length)
echo "open SupportSupervisorWorkflow runs before starting v0.5 Workers: $open_old (v0.3 runs would not replay)"

step "4. payments-mock, Finance Worker (scripted), the finance-agent endpoint"
"$PY" -m payments_mock.server >"$LOGS/payments.log" 2>&1 & pids+=("$!")
for _ in $(seq 1 40); do curl -sf "$PAYMENTS_URL/healthz" >/dev/null && break; sleep 0.25; done
start_finance scripted "$PY" scripts/scripted_finance_worker.py --delay 0.3
scripts/nexus-endpoint.sh apply

step "5. crash safety with the A2A tool: kill -9 the Support Worker mid sub-agent turn (scripted)"
wf="support-crash-scripted-$TS"
start_support crash-a "$PY" scripts/scripted_worker.py --delay 2
scripts/demo/case-a.sh "$wf" >"$LOGS/$wf.log" 2>&1 & starter=$!; pids+=("$starter")
for _ in $(seq 1 240); do
  if temporal workflow show --namespace agents --workflow-id "$wf" -o json 2>/dev/null \
       | jq -e '[.events[] | select(.eventType=="EVENT_TYPE_ACTIVITY_TASK_COMPLETED")] | length >= 3' >/dev/null &&
     temporal workflow describe --namespace agents --workflow-id "$wf" -o json \
       | jq -e '[.pendingActivities[]? | select(.activityType.name=="invoke_model"
                 and .state=="PENDING_ACTIVITY_STATE_STARTED")] | length > 0' >/dev/null; then
    break
  fi
  sleep 0.5
done
kill -9 "$support_pid"; wait "$support_pid" 2>/dev/null || true
echo "worker A killed with SIGKILL during an in-flight invoke_model (after lookup_order completed)"
support_pid=""
start_support crash-b "$PY" scripts/scripted_worker.py --delay 0.3
t0=$SECONDS
wait "$starter"
echo "Case A finished $((SECONDS - t0)) s after the kill"
"${UV[@]}" python scripts/check_a2a_run.py "$wf" completed
"${UV[@]}" python scripts/history_report.py "$wf" | tee "$LOGS/crash-history.txt"
[[ "$(grep -c 'attempts=2' "$LOGS/crash-history.txt")" == "1" ]]
grep -Eq "lookup_order +attempts=1 completed" "$LOGS/crash-history.txt"
echo "crash: only the interrupted model turn was retried; lookup_order and the A2A hop ran once"
record "$wf" support-crash-scripted

step "6. Case A, tool mode (scripted): Supervisor -> ask_finance_agent -> Nexus -> Finance"
wf="support-1042-scripted"
run_case case-a "$wf" tool completed
"${UV[@]}" python scripts/timeline.py "$wf" | tee "$LOGS/timeline-1042.txt"
record "$wf" support-1042-scripted

step "7. Case A, handoff mode (scripted): same answer, Nexus call from run()"
wf="support-1042-handoff-scripted"
run_case case-a "$wf" handoff completed
record "$wf" support-1042-handoff-scripted
echo "make task-state WF=$wf:"
scripts/demo/task-state.sh "$wf" | jq -c '{id, state, artifacts: [.artifacts[].name]}'

step "8. Case B (scripted): \$450 is over Finance's auto-approval limit -> rejected (v0.6: approval)"
wf="support-2077-scripted"
run_case case-b "$wf" tool rejected
record "$wf" support-2077-scripted

if [[ "${VERIFY_SKIP_BEDROCK:-0}" == "1" ]]; then
  step "9-10. live Opus 5.5 (Support and Finance): SKIPPED (VERIFY_SKIP_BEDROCK=1)"
else
  step "9. live: Finance and Support Workers on Opus 5.5 (make worker-finance, make worker-support)"
  start_finance live "$PY" -m finance_agent.worker
  start_support live "$PY" -m support_agent.worker
  grep -h "model=" "$LOGS"/support-live.log "$LOGS"/finance-live.log | sed -E 's/^[0-9-]+ [0-9:,]+ //' || true
  wf="support-1042"
  run_case case-a "$wf" tool completed
  "${UV[@]}" python scripts/timeline.py "$wf" | tee "$LOGS/timeline-live-1042.txt"
  record "$wf" support-1042

  step "10. live Case B"
  wf="support-2077"
  run_case case-b "$wf" tool rejected
  record "$wf" support-2077
fi
stop_support

printf '\nVERIFY OK\n'
