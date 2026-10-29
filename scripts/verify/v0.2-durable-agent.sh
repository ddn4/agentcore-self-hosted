#!/usr/bin/env bash
# Observable acceptance checks for v0.2-durable-agent (IMPLEMENTATION_PLAN.md v0.2).
# Output is piped through scripts/redact.sh so it can be pasted into the story doc.
#   VERIFY_SKIP_BEDROCK=1  skip the live Opus 5.5 steps (spikes, live Case A + crash)
#   RECORD_HISTORY=1       re-record tests/histories/support-1042.json from the live run
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
exec > >(scripts/redact.sh) 2>&1

export TEMPORAL_CONFIG_FILE="$PWD/config/temporal.toml" TEMPORAL_PROFILE=local
export SUPPORT_TASK_QUEUE="${SUPPORT_TASK_QUEUE:-support-dev-tq}"
UV=(uv run --frozen)
# Workers are started with the venv's python directly, not `uv run`: `uv run`
# spawns python as a child, so `kill -9 $!` would hit uv and orphan the Worker.
PY=.venv/bin/python
LOGS=.temporal/verify-v0.2
mkdir -p "$LOGS"

step() { printf '\n=== %s\n' "$*"; }
started_server=0
pids=()
cleanup() {
  for p in "${pids[@]:-}"; do [[ -n "$p" ]] && kill "$p" 2>/dev/null || true; done
  [[ $started_server -eq 1 ]] && scripts/dev-server.sh stop || true
}
trap cleanup EXIT

# Wait until the Workflow has a *started* invoke_model Activity after lookup_order
# completed, i.e. the Worker is in the middle of the Supervisor's second LLM turn.
wait_mid_model_call() {
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
  echo "timed out waiting for an in-flight invoke_model on $wf" >&2
  return 1
}

# crash_demo <label> <workflow-id> <worker command...>
# Starts worker A, starts Case A, kill -9's worker A mid-invoke_model, starts
# worker B, and waits for the reply. Prints the Activity attempts table.
crash_demo() {
  local label="$1" wf="$2"; shift 2
  "$@" >"$LOGS/$label-worker-a.log" 2>&1 & local a=$!; pids+=("$a")
  sleep 3
  scripts/demo/case-a.sh "$wf" >"$LOGS/$label-case-a.log" 2>&1 & local starter=$!; pids+=("$starter")
  wait_mid_model_call "$wf"
  kill -9 "$a"
  echo "worker A (pid $a) killed with SIGKILL during invoke_model (Supervisor turn 2)"
  "$@" >"$LOGS/$label-worker-b.log" 2>&1 & local b=$!; pids+=("$b")
  echo "worker B (pid $b) started; the server re-dispatches the turn after the 10 s heartbeat timeout"
  local t0=$SECONDS
  wait "$starter"
  echo "Case A finished $((SECONDS - t0)) s after the kill. Reply:"
  grep -v '^waiting for' "$LOGS/$label-case-a.log" | sed 's/^/  /'
  kill "$b" 2>/dev/null || true; wait "$b" 2>/dev/null || true
  "${UV[@]}" python scripts/history_report.py "$wf" | tee "$LOGS/$label-history.txt"
  grep -Eq "^  3\. invoke_model +attempts=2 completed" "$LOGS/$label-history.txt"
  grep -Eq "^  2\. lookup_order +attempts=1 completed" "$LOGS/$label-history.txt"
  echo "$label: only the interrupted model turn was retried; lookup_order ran once"
}

step "1. make check (lint, types, unit, workflow, integration, infra-static, scrub-check)"
log=$LOGS/check.log
make -s check >"$log" 2>&1 || { cat "$log"; exit 1; }
grep -E "passed|All checks|0 errors|infra-static|scrub-check|make check" "$log"

step "2. two-namespace dev server"
if scripts/dev-server.sh status >/dev/null 2>&1; then
  echo "dev server already running; using it"
else
  scripts/dev-server.sh start
  started_server=1
fi

step "3. crash safety, offline: kill -9 a real Worker process mid-invoke_model (scripted model)"
crash_demo scripted "support-crash-scripted-$(date +%s)" "$PY" scripts/scripted_worker.py --delay 4

if [[ "${VERIFY_SKIP_BEDROCK:-0}" == "1" ]]; then
  step "4-6. live Opus 5.5 steps: SKIPPED (VERIFY_SKIP_BEDROCK=1)"
else
  step "4. spike: what goes on the wire, rejections, effort field and latency"
  RUN_BEDROCK_TESTS=1 "${UV[@]}" python spikes/opus_params.py --turns 3

  step "5. spike: structured output vs forced tool_choice"
  RUN_BEDROCK_TESTS=1 "${UV[@]}" python spikes/opus_structured_output.py

  step "6. live Case A on Opus 5.5 with a kill -9 mid-run (make worker-support + make case-a)"
  wf=support-1042
  temporal workflow terminate --namespace agents --workflow-id "$wf" --reason "verify reset" >/dev/null 2>&1 || true
  crash_demo live "$wf" "$PY" -m support_agent.worker
  reply="$(cat "$LOGS/live-case-a.log")"
  grep -q '40' <<<"$reply" && grep -qi 'eligib' <<<"$reply"
  echo "reply mentions \$40 and eligibility"
  grep -h "support worker:" "$LOGS"/live-worker-a.log | sed 's/^.*support worker:/support worker:/'
  if [[ "${RECORD_HISTORY:-0}" == "1" ]]; then
    "${UV[@]}" python scripts/record_history.py "$wf"
    "${UV[@]}" pytest -q services/support-agent/tests/test_replay.py
  fi
fi

step "7. scrub-check (tree and history)"
scripts/scrub-check.sh
scripts/scrub-check.sh --history

step "v0.2-durable-agent: VERIFY OK"
