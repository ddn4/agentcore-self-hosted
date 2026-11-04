#!/usr/bin/env bash
# Observable acceptance checks for v0.7-serverless-shape (tag v0.7).
# Output is piped through scripts/redact.sh so it can be pasted into the story doc.
#   VERIFY_SKIP_CHECK=1      skip step 1 (make check)
#   VERIFY_SKIP_BEDROCK=1    skip step 12 (live: one PingWorkflow + Case A on Opus 5.5 via the WCI)
# Needs a committed tree (scripts/build-id.sh refuses dirty Support inputs). Starts its own dev
# server (WCI=1: the dev server's WCI, AWS SDK pointed at the local AgentCore emulator; no AWS),
# the emulator, payments-mock, the scripted Finance Worker and the AgentCore-shaped Support app,
# and stops everything it started on exit. Wipes the dev server's state first (.temporal/dev.db).
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
exec > >(scripts/redact.sh) 2>&1

export TEMPORAL_CONFIG_FILE="$PWD/config/temporal.toml" TEMPORAL_PROFILE=local
export PAYMENTS_URL=http://127.0.0.1:8081
export AGENTCORE_DEBOUNCE_SECONDS="${AGENTCORE_DEBOUNCE_SECONDS:-20}"
unset FINANCE_APPROVAL_TIMEOUT SUPPORT_NEXUS_SEND_TIMEOUT TEMPORAL_TASK_QUEUE TEMPORAL_BUILD_ID
UV=(uv run --frozen)
PY=.venv/bin/python
LOGS=.temporal/verify-v0.7
rm -rf "$LOGS"; mkdir -p "$LOGS"
EMU_LOG="$LOGS/agentcore-emulator.jsonl"
NS=agents
DEP=support-supervisor
step() { printf '\n=== %s\n' "$*"; }
pids=()
app_pid=""
cleanup() {
  for p in ${pids[@]+"${pids[@]}"}; do kill "$p" 2>/dev/null || true; done
  for p in ${pids[@]+"${pids[@]}"}; do wait "$p" 2>/dev/null || true; done
  scripts/dev-server.sh stop >/dev/null 2>&1 || true
}
trap cleanup EXIT
ping_status() { curl -s http://127.0.0.1:8080/ping | jq -r .status; }
wait_ping() { # wait_ping <status> [seconds]
  local want="$1" limit="${2:-90}" now=""
  for _ in $(seq 1 $((limit * 2))); do now="$(ping_status)"; [[ "$now" == "$want" ]] && return 0; sleep 0.5; done
  echo "FAIL: /ping is '$now', expected '$want'"; return 1
}
task_state() {
  temporal workflow query --namespace agent-services --workflow-id "a2a-task-$1" --type get_task \
    -o json 2>/dev/null | jq -r '.queryResult[0].status.state' 2>/dev/null || echo "-"
}
wait_state() { # wait_state <support-workflow-id> <state> [seconds]
  local now="-"
  for _ in $(seq 1 $(( ${3:-120} * 2 ))); do now="$(task_state "$1")"; [[ "$now" == "$2" ]] && { echo "a2a-task-$1 is $2"; return 0; }; sleep 0.5; done
  echo "FAIL: a2a-task-$1 is '$now', expected '$2'"; return 1
}
invokes() { [[ -f "$EMU_LOG" ]] && grep -c '"op": "InvokeAgentRuntime"' "$EMU_LOG" || echo 0; }
lifetime_lines() { grep -aE "lifetime [0-9]+: (polling|idle)|worker lifetime [0-9]+ over" "$LOGS/app.log" | sed -E 's/^[0-9-]+ ([0-9:]+),[0-9]+ INFO support_agent.agentcore: /  \1 /; s/ identity .*//'; }
start_app() { # start_app <label> <command...>  (the AgentCore-shaped Support app on :8080)
  local label="$1"; shift
  "$@" >>"$LOGS/app.log" 2>&1 & app_pid=$!; pids+=("$app_pid")
  for _ in $(seq 1 60); do curl -sf http://127.0.0.1:8080/ping >/dev/null && break; sleep 0.25; done
  echo "AgentCore app ($label) on :8080, /ping $(ping_status)"
}
stop_app() { [[ -n "$app_pid" ]] && { kill "$app_pid" 2>/dev/null || true; wait "$app_pid" 2>/dev/null || true; }; app_pid=""; }

step "1. make check"
if [[ "${VERIFY_SKIP_CHECK:-0}" == "1" ]]; then echo "SKIPPED (VERIFY_SKIP_CHECK=1)"; else
  make -s check >"$LOGS/check.log" 2>&1 || { cat "$LOGS/check.log"; exit 1; }
  grep -E "passed|0 errors|scrub-check|make check" "$LOGS/check.log"
fi

step "2. offline: entrypoint contract, debounce (fake clock), PingWorkflow on a versioned Worker, Build ID"
TESTS=(services/support-agent/tests/test_agentcore_app.py services/support-agent/tests/test_activity_tracker.py
       services/support-agent/tests/test_serverless_worker.py
       tests/test_build_id.py tests/test_agentcore_emulator.py)
"${UV[@]}" pytest -q -rA "${TESTS[@]}" 2>&1 | grep -E '^PASSED' | sed -E 's/^PASSED [^:]*::/  /'
"${UV[@]}" pytest -q "${TESTS[@]}" 2>&1 | tail -1

step "3. Build ID (content hash of Support + contract + Support's dependency set)"
BUILD_ID="$(scripts/build-id.sh)"
export TEMPORAL_BUILD_ID="$BUILD_ID"
echo "Build ID $BUILD_ID at $(git rev-parse --short HEAD) (SUPPORT_GIT_SHA, metadata only)"
echo "docs-only / Finance-only commits keep it; a Support commit changes it; dirty Support refuses:"
"${UV[@]}" pytest -q tests/test_build_id.py 2>&1 | tail -1

step "4. Does the dev server contain the WCI? (CLI $(temporal --version | awk '{print $3}'))"
echo "worker deployment subcommands: $(temporal worker deployment --help | awk '/Available Commands/{f=1;next} f&&/^ +[a-z]/{print $1} /^Flags/{f=0}' | tr '\n' ' ')"
scripts/dev-server.sh stop >/dev/null 2>&1 || true
rm -f .temporal/dev.db .temporal/dev.db-*
WCI=1 scripts/dev-server.sh start
"$PY" scripts/agentcore_emulator.py --ack-only --log "$LOGS/probe-emulator.jsonl" >"$LOGS/probe-emulator.out" 2>&1 & emu_pid=$!; pids+=("$emu_pid")
sleep 1; head -1 "$LOGS/probe-emulator.out"
echo "(step 5 probes use the emulator in --ack-only mode: it acknowledges invocations itself)"

step "5. 7.1: is 'worker deployment create' required? (throwaway deployments)"
probe="probe-$(date +%s)"
echo "+ create-version on a deployment that was never created (no compute config):"
temporal worker deployment create-version -n "$NS" --deployment-name "$probe-a" --build-id p1 2>&1 | sed 's/^/  /' || true
echo "+ the same with an AgentCore compute config (emulator endpoint):"
ARN="arn:aws:bedrock-agentcore:us-east-1:$(printf '0%.0s' {1..12}):runtime/support_supervisor_local/runtime-endpoint/b_probe"
temporal worker deployment create-version -n "$NS" --deployment-name "$probe-b" --build-id p1 \
  --aws-agentcore-endpoint-arn "$ARN" --aws-agentcore-skip-role-and-external-id 2>&1 | sed 's/^/  /' || true
echo "+ create, then create-version with the compute config:"
temporal worker deployment create -n "$NS" --name "$probe-c" 2>&1 | sed 's/^/  /'
temporal worker deployment create-version -n "$NS" --deployment-name "$probe-c" --build-id p1 \
  --aws-agentcore-endpoint-arn "$ARN" --aws-agentcore-skip-role-and-external-id 2>&1 | sed 's/^/  /'
echo "  emulator saw: $(jq -c '[.op, (.payload // "")]' "$LOGS/probe-emulator.jsonl" | tr '\n' ' ')"
echo "  (create-version itself invokes the runtime once: GetAgentRuntimeEndpoint, then InvokeAgentRuntime)"
echo "+ Q13: set-current-version with no Worker ever polling, compute config present, no --allow-no-pollers:"
temporal worker deployment set-current-version -n "$NS" --deployment-name "$probe-c" --build-id p1 --yes 2>&1 | sed 's/^/  /' || true
echo "+ Q13: a version with NO compute config that never had a poller (a Build ID nothing ran):"
temporal worker deployment create -n "$NS" --name "$probe-d" >/dev/null
temporal worker deployment set-current-version -n "$NS" --deployment-name "$probe-d" --build-id never-polled --yes 2>&1 | sed 's/^/  /' || true
temporal worker deployment set-current-version -n "$NS" --deployment-name "$probe-d" --build-id never-polled --yes --allow-no-pollers 2>&1 | sed 's/^/  /' || true
kill "$emu_pid" 2>/dev/null || true; wait "$emu_pid" 2>/dev/null || true
echo "+ create-version when the runtime cannot be reached (emulator forwards to :8080, nothing there):"
"$PY" scripts/agentcore_emulator.py --log "$LOGS/probe-emulator.jsonl" >>"$LOGS/probe-emulator.out" 2>&1 & emu_pid=$!; pids+=("$emu_pid")
sleep 1
temporal worker deployment create-version -n "$NS" --deployment-name "$probe-c" --build-id p2 \
  --aws-agentcore-endpoint-arn "$ARN" --aws-agentcore-skip-role-and-external-id 2>&1 | sed 's/^/  /' || true
echo "  emulator: $(tail -1 "$LOGS/probe-emulator.jsonl" | jq -c '{op, status, answer: .answer.message[0:60]}')"
kill "$emu_pid" 2>/dev/null || true; wait "$emu_pid" 2>/dev/null || true
"$PY" scripts/agentcore_emulator.py --log "$EMU_LOG" >"$LOGS/emulator.out" 2>&1 & pids+=("$!")
sleep 1; head -1 "$LOGS/emulator.out"

step "6. Finance side (scripted), Nexus endpoint; the AgentCore-shaped Support app (scripted model)"
"$PY" -m payments_mock.server >"$LOGS/payments.log" 2>&1 & pids+=("$!")
for _ in $(seq 1 40); do curl -sf "$PAYMENTS_URL/healthz" >/dev/null && break; sleep 0.25; done
"$PY" scripts/scripted_finance_worker.py --delay 0.3 >"$LOGS/finance.log" 2>&1 & pids+=("$!")
sleep 2
scripts/nexus-endpoint.sh apply | tail -1
start_app scripted "$PY" scripts/scripted_agentcore_app.py --delay 0.3
grep -a "AgentCore app:" "$LOGS/app.log" | sed -E 's/^.*AgentCore app: /  /'

step "7. Part A, 'I am the WCI' (no compute config: the WCI stays out of it): register, Case B, drain"
scripts/invoke-local.sh
scripts/register-version.sh --poller
echo "/ping $(ping_status) while lifetime 1 polls"
wf=support-2077-sl-manual
temporal workflow terminate -n "$NS" -w "$wf" --reason "verify reset" >/dev/null 2>&1 || true
CASE_B_WAIT=0 SUPPORT_TASK_QUEUE=support-tq scripts/demo/case-b.sh "$wf" | grep -E '^started'
wait_state "$wf" input-required
echo "Finance waits for a human; Support's Worker has nothing to do: waiting for the ${AGENTCORE_DEBOUNCE_SECONDS}s drain"
wait_ping Healthy 90
echo "/ping $(ping_status) after the drain"
lifetime_lines

step "8. Part A: approve while no Worker runs: backlog on support-tq, no pollers; then invoke by hand"
T_APPROVE_A="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
scripts/demo/approve.sh "$wf" dana.finance "photos checked" | tail -1
sleep 3
temporal worker deployment describe-version -n "$NS" --deployment-name "$DEP" --build-id "$BUILD_ID" \
  --report-task-queue-stats -o json \
  | jq -c '[.taskQueuesInfos[] | {queue: .name, type, backlog: .stats.approximateBacklogCount}]' | sed 's/^/  stats: /'
now=$(date +%s)
recent=$(temporal task-queue describe -n "$NS" -t support-tq --legacy-mode -o json \
  | jq --argjson now "$now" '[.pollers[]? | select(($now - .last_access_time.seconds) < 5)] | length')
echo "  pollers seen in the last 5 s: $recent; Workflow: $(temporal workflow describe -n "$NS" -w "$wf" -o json | jq -r .workflowExecutionInfo.status)"
echo "  WCI invocations since the approval: $(invokes) (no compute config on this version)"
scripts/invoke-local.sh | tail -2
temporal workflow result -n "$NS" -w "$wf" 2>&1 | grep -E '^ *Result ' | cut -c1-200
"${UV[@]}" python scripts/serverless_report.py "$wf" --min-lifetimes 2 --build-id "$BUILD_ID"
wait_ping Healthy 90
echo "+ set-current-version again after the drain (no pollers now), without --allow-no-pollers:"
temporal worker deployment set-current-version -n "$NS" --deployment-name "$DEP" --build-id "$BUILD_ID" --yes 2>&1 | sed 's/^/  /'

step "9. Part B, the dev server's own WCI: add the AgentCore compute config (local emulator endpoint)"
scripts/register-version.sh --local-wci
sleep 1
echo "  emulator: $(jq -r .op "$EMU_LOG" | sort | uniq -c | tr -s ' ' | tr '\n' ';')"
wait_ping Healthy 90
echo "PingWorkflow with no Worker running (cold start):"
before=$(invokes)
temporal workflow execute -n "$NS" -t support-tq --type PingWorkflow -w ping-1 --input '"cold start"' \
  2>&1 | grep -E '^ *Result ' | cut -c1-160
echo "  WCI invocations for it: $(( $(invokes) - before ))"

step "10. Part B: Case B with nobody invoking by hand: WCI -> session, drain, approve -> WCI -> session"
wf=support-2077-sl
temporal workflow terminate -n "$NS" -w "$wf" --reason "verify reset" >/dev/null 2>&1 || true
wait_ping Healthy 90
before=$(invokes)
CASE_B_WAIT=0 SUPPORT_TASK_QUEUE=support-tq scripts/demo/case-b.sh "$wf" | grep -E '^started'
wait_state "$wf" input-required
wait_ping Healthy 90
echo "drained; WCI invocations so far for this conversation: $(( $(invokes) - before ))"
T_APPROVE="$(date -u +%Y-%m-%dT%H:%M:%S.000Z)"
echo "approve at $T_APPROVE (no Worker running; nobody will invoke by hand)"
scripts/demo/approve.sh "$wf" dana.finance "photos checked" | tail -1
res="$LOGS/$wf.result"
status=""
for _ in $(seq 1 120); do
  status="$(temporal workflow describe -n "$NS" -w "$wf" -o json | jq -r .workflowExecutionInfo.status)"
  [[ "$status" != WORKFLOW_EXECUTION_STATUS_RUNNING ]] && break
  sleep 0.5
done
if [[ "$status" == WORKFLOW_EXECUTION_STATUS_RUNNING ]]; then
  echo "Q15 (local): NO WCI invocation after the Nexus completion within 60 s; invoking by hand"
  scripts/invoke-local.sh | tail -1
else
  echo "Q15 (local): the conversation finished with no manual invocation ($status)"
fi
temporal workflow result -n "$NS" -w "$wf" >"$res" 2>&1
grep -E '^ *Result ' "$res" | cut -c1-200
echo "emulator calls after the approval:"
jq -c --arg t "$T_APPROVE" 'select(.time >= $t) | {time, op, session, payload, answer: .answer.message, lifetime: .answer.lifetime}' "$EMU_LOG" | sed 's/^/  /'
"${UV[@]}" python scripts/serverless_report.py "$wf" --min-lifetimes 2 --build-id "$BUILD_ID"
"${UV[@]}" python scripts/check_a2a_run.py "$wf" completed --approver dana.finance | tail -3
echo "the WCI's own history since Part B began:"
scripts/wci-history.sh "$BUILD_ID" | sed -n '1p;/InvokeWorker activities/p'
scripts/wci-history.sh "$BUILD_ID" | grep -E "signal|InvokeWorker$|update|ValidateSpec" | tail -12
echo "Worker lifetimes of the AgentCore app (this verify run):"
lifetime_lines
wait_ping Healthy 90
echo "/ping $(ping_status): no Worker left running"

if [[ "${VERIFY_SKIP_BEDROCK:-0}" == "1" ]]; then step "12. live Opus 5.5 through the WCI: SKIPPED (VERIFY_SKIP_BEDROCK=1)"; else
  step "12. live: the real AgentCore app (Opus 5.5) invoked by the WCI: Case A"
  stop_app
  start_app live "${UV[@]}" python -m support_agent.agentcore_app
  wf=support-1042-sl
  temporal workflow terminate -n "$NS" -w "$wf" --reason "verify reset" >/dev/null 2>&1 || true
  SUPPORT_TASK_QUEUE=support-tq scripts/demo/case-a.sh "$wf" 2>&1 | grep -E '^ *Result ' | cut -c1-240
  "${UV[@]}" python scripts/serverless_report.py "$wf" --build-id "$BUILD_ID"
  "${UV[@]}" python scripts/check_a2a_run.py "$wf" completed | tail -2
fi
stop_app

printf '\nVERIFY OK\n'
