#!/usr/bin/env bash
# Observable acceptance checks for v0.6-human-in-the-loop (IMPLEMENTATION_PLAN.md v0.6).
# Output is piped through scripts/redact.sh so it can be pasted into the story doc.
#   VERIFY_SKIP_BEDROCK=1        skip the live Opus 5.5 step (10); everything else is offline
#   VERIFY_REAL_WAIT_MINUTES=N   step 9: approve after N real minutes with Support stopped
#                                (plan: >= 30; default 0 = skip)
#   RECORD_HISTORY=1             re-record the replay fixtures (support-2077-approved-scripted in
#                                services/support-agent/tests/histories, a2a-task-support-2077-
#                                approved-scripted in services/finance-agent/tests/histories)
#   VERIFY_SKIP_CHECK=1          skip step 1 (only to bootstrap fixtures that make check replays)
# Starts its own dev server (unless one is running), payments-mock, Finance and Support Workers,
# and stops everything it started on exit.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
exec > >(scripts/redact.sh) 2>&1

export TEMPORAL_CONFIG_FILE="$PWD/config/temporal.toml" TEMPORAL_PROFILE=local
export SUPPORT_TASK_QUEUE="${SUPPORT_TASK_QUEUE:-support-dev-tq}"
export PAYMENTS_URL=http://127.0.0.1:8081
unset FINANCE_APPROVAL_TIMEOUT SUPPORT_NEXUS_SEND_TIMEOUT
UV=(uv run --frozen)
# Workers run on the venv's python directly (not `uv run`), so kill hits the Worker itself.
PY=.venv/bin/python
LOGS=.temporal/verify-v0.6
rm -rf "$LOGS"; mkdir -p "$LOGS"
RECORD="${RECORD_HISTORY:-0}"
WAIT_MIN="${VERIFY_REAL_WAIT_MINUTES:-0}"

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

record() { # record <workflow-id> <fixture-name>   (Support side, then Finance's task)
  [[ "$RECORD" == "1" ]] || return 0
  "${UV[@]}" python scripts/record_history.py "$1" --name "$2"
  "${UV[@]}" python scripts/record_history.py "a2a-task-$1" --name "a2a-task-$2" \
    --namespace agent-services --out services/finance-agent/tests/histories
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
task_state() { # task_state <support-workflow-id>
  temporal workflow query --namespace agent-services --workflow-id "a2a-task-$1" --type get_task \
    -o json 2>/dev/null | jq -r '.queryResult[0].status.state' 2>/dev/null || echo "-"
}
wait_state() { # wait_state <support-workflow-id> <state> [seconds]
  local wf="$1" want="$2" limit="${3:-180}" now="-"
  for _ in $(seq 1 $((limit * 2))); do
    now="$(task_state "$wf")"; [[ "$now" == "$want" ]] && { echo "a2a-task-$wf is $want"; return 0; }
    sleep 0.5
  done
  echo "FAIL: a2a-task-$wf is '$now', expected '$want'"; return 1
}
start_case_b() { # start_case_b <workflow-id> [mode]
  temporal workflow terminate --namespace agents --workflow-id "$1" --reason "verify reset" >/dev/null 2>&1 || true
  temporal workflow terminate --namespace agent-services --workflow-id "a2a-task-$1" --reason "verify reset" >/dev/null 2>&1 || true
  SUPPORT_A2A_MODE="${2:-tool}" scripts/demo/case-b.sh "$1" | grep -E '^started'
}
show_result() { # show_result <workflow-id>
  temporal workflow result --namespace agents --workflow-id "$1" >"$LOGS/$1.result" 2>&1
  grep -E '^ *Result' "$LOGS/$1.result" | sed 's/^/  /'
}

step "1. make check (lint, types, unit, workflow, integration, infra-static, scrub-check)"
if [[ "${VERIFY_SKIP_CHECK:-0}" == "1" ]]; then
  echo "SKIPPED (VERIFY_SKIP_CHECK=1)"
else
  make -s check >"$LOGS/check.log" 2>&1 || { cat "$LOGS/check.log"; exit 1; }
  grep -E "passed|0 errors|infra-static|scrub-check|make check" "$LOGS/check.log"
fi

step "2. offline: Finance state machine (24 h skipped), Supervisor handling, two-namespace paths"
TESTS=(services/finance-agent/tests/test_task_workflow.py services/support-agent/tests/test_workflow.py
       tests/test_supervisor_a2a.py tests/test_approval_timeout_a2a.py tests/test_timeout_budget.py
       tests/test_a2a_nexus.py)
"${UV[@]}" pytest -q -p no:randomly "${TESTS[@]}" -rA 2>&1 | grep -E '^PASSED' \
  | sed -E 's/^PASSED [^:]*::/  /' \
  | grep -E 'approv|reject|timeout|budget|risk|signal|input|waits|stopped|nobody|human|orphan|caller|2077' || true
"${UV[@]}" pytest -q "${TESTS[@]}" 2>&1 | tail -1

step "3. dev server (both namespaces)"
if scripts/dev-server.sh status >/dev/null 2>&1; then
  echo "dev server already running; using it"
else
  scripts/dev-server.sh start
  started_server=1
fi

step "4. payments-mock, Finance Worker (scripted, FINANCE_APPROVAL_TIMEOUT default 24h), endpoint"
"$PY" -m payments_mock.server >"$LOGS/payments.log" 2>&1 & pids+=("$!")
for _ in $(seq 1 40); do curl -sf "$PAYMENTS_URL/healthz" >/dev/null && break; sleep 0.25; done
start_finance scripted "$PY" scripts/scripted_finance_worker.py --delay 0.3
grep -ho "approval_timeout=[^ ]*" "$LOGS/finance-scripted.log" || true
scripts/nexus-endpoint.sh apply
start_support scripted "$PY" scripts/scripted_worker.py --delay 0.3

step "5. Case B (scripted): input-required; Support Worker STOPPED; approve; a new Support Worker finishes"
wf="support-2077-approved-scripted"
start_case_b "$wf"
wait_state "$wf" input-required
echo "make task-state WF=$wf:"
scripts/demo/task-state.sh "$wf" | jq -c '{id, state, message: (.message[0:110] + "...")}'
echo "make pending WF=$wf:"
make -s pending WF="$wf" | tee "$LOGS/pending.json" | jq -c .
jq -e '.pendingNexusOperations | length == 1 and .[0].operation == "send_message"
       and .[0].state == "PENDING_NEXUS_OPERATION_STATE_STARTED"
       and .[0].scheduleToCloseTimeout == "90000s"' "$LOGS/pending.json" >/dev/null
echo "  ok   one pending Nexus operation: send_message, started, 25 h budget (90000s)"
stop_support
echo "Support Worker stopped (no poller on $SUPPORT_TASK_QUEUE)"
scripts/demo/approve.sh "$wf" dana.finance "photos checked"
sleep 2
status=$(temporal workflow describe --namespace agents --workflow-id "$wf" -o json | jq -r '.workflowExecutionInfo.status')
done_op=$(temporal workflow show --namespace agents --workflow-id "$wf" -o json \
  | jq '[.events[] | select(.eventType=="EVENT_TYPE_NEXUS_OPERATION_COMPLETED")] | length')
echo "Support while stopped: $status, NexusOperationCompleted events: $done_op (card + send_message)"
[[ "$status" == "WORKFLOW_EXECUTION_STATUS_RUNNING" && "$done_op" == "2" ]]
start_support scripted-2 "$PY" scripts/scripted_worker.py --delay 0.3
show_result "$wf"
"${UV[@]}" python scripts/check_a2a_run.py "$wf" completed --approver dana.finance
"${UV[@]}" python scripts/approval_report.py "$wf"
record "$wf" support-2077-approved-scripted

step "6. Case B, handoff mode (scripted): same approval, Nexus call from run()"
wf="support-2077-handoff-scripted-v06"
start_case_b "$wf" handoff
wait_state "$wf" input-required
scripts/demo/approve.sh "$wf" dana.finance
show_result "$wf"
"${UV[@]}" python scripts/check_a2a_run.py "$wf" completed --mode handoff --approver dana.finance

step "7. Case B rejected by the approver (scripted): apology, no refund"
wf="support-2077-rejected-scripted"
start_case_b "$wf"
wait_state "$wf" input-required
scripts/demo/reject.sh "$wf" dana.finance "duplicate claim"
show_result "$wf"
"${UV[@]}" python scripts/check_a2a_run.py "$wf" rejected
echo "approve.sh on a closed task is refused:"
if scripts/demo/approve.sh "$wf" dana.finance 2>&1; then echo "FAIL: approve on a closed task"; exit 1; fi

step "8. Timeout path, real processes: FINANCE_APPROVAL_TIMEOUT=5s, SUPPORT_NEXUS_SEND_TIMEOUT=60s"
start_finance timeout env FINANCE_APPROVAL_TIMEOUT=5s "$PY" scripts/scripted_finance_worker.py --delay 0.3
grep -ho "approval_timeout=[^ ]*" "$LOGS/finance-timeout.log" || true
wf="support-2077-timeout-scripted"
SUPPORT_NEXUS_SEND_TIMEOUT=60s start_case_b "$wf"
show_result "$wf"
"${UV[@]}" python scripts/check_a2a_run.py "$wf" failed
temporal workflow show --namespace agents --workflow-id "$wf" -o json \
  | jq -r '.events[] | select(.eventType | test("NEXUS_OPERATION_(COMPLETED|FAILED|TIMED_OUT)")) | .eventType' \
  | sort | uniq -c | sed 's/^/  /'
echo "  (send_message COMPLETED with an A2A failed task: no NexusOperationTimedOut, ADR-0009)"
start_finance scripted-2 "$PY" scripts/scripted_finance_worker.py --delay 0.3

if [[ "$WAIT_MIN" -gt 0 ]]; then
  step "9. Real time: approve after $WAIT_MIN min with the Support Worker stopped (plan: >= 30 min; Q19b)"
  wf="support-2077-long-wait"
  start_support long "$PY" scripts/scripted_worker.py --delay 0.3
  start_case_b "$wf"
  wait_state "$wf" input-required
  stop_support
  echo "$(date -u +%H:%M:%SZ) Support Worker stopped; waiting $WAIT_MIN min"
  for ((m = 1; m <= WAIT_MIN; m++)); do
    sleep 60
    if (( m % 10 == 0 || m == WAIT_MIN )); then
      echo "$(date -u +%H:%M:%SZ) +${m} min: Finance task $(task_state "$wf"); Support $(temporal workflow describe \
        --namespace agents --workflow-id "$wf" -o json | jq -r '.workflowExecutionInfo.status')"
    fi
  done
  scripts/demo/approve.sh "$wf" dana.finance "approved after ${WAIT_MIN} min"
  start_support long-2 "$PY" scripts/scripted_worker.py --delay 0.3
  show_result "$wf"
  "${UV[@]}" python scripts/check_a2a_run.py "$wf" completed --approver dana.finance
  "${UV[@]}" python scripts/approval_report.py "$wf" | tee "$LOGS/long-wait.txt"
else
  step "9. Real-time wait: SKIPPED (set VERIFY_REAL_WAIT_MINUTES=30 or more)"
fi

if [[ "${VERIFY_SKIP_BEDROCK:-0}" == "1" ]]; then
  step "10. live Opus 5.5 Case B with a CLI approve: SKIPPED (VERIFY_SKIP_BEDROCK=1)"
else
  step "10. live: Finance and Support on Opus 5.5; Case B; make approve"
  start_finance live "$PY" -m finance_agent.worker
  start_support live "$PY" -m support_agent.worker
  grep -h "model=" "$LOGS"/support-live.log "$LOGS"/finance-live.log | sed -E 's/^[0-9-]+ [0-9:,]+ //' || true
  wf="support-2077"
  start_case_b "$wf"
  wait_state "$wf" input-required 300
  scripts/demo/task-state.sh "$wf" | jq -c '{id, state}'
  scripts/demo/approve.sh "$wf" dana.finance
  show_result "$wf"
  "${UV[@]}" python scripts/check_a2a_run.py "$wf" completed --approver dana.finance
  [[ "$RECORD" == "1" ]] && record "$wf" support-2077-approved
fi
stop_support

printf '\nVERIFY OK\n'
