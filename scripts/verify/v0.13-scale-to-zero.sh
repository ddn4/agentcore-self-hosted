#!/usr/bin/env bash
# Observable acceptance checks for v0.13-scale-to-zero (IMPLEMENTATION_PLAN.md v0.13; runbook §1.3,
# Phase 8 steps 6-7, §5 "End to end"). us-east-1, default SSO profile. Output goes through
# scripts/redact.sh.
#
# It creates NO AWS resources. It uses what v0.10-v0.12 left running (EKS + Temporal + Finance,
# Support's AgentCore VPC runtime, current version on EKS). AgentCore sessions are started only by
# the server's Worker Controller (WCI); they cost per second of use, and every Case costs Bedrock
# tokens: 2 Case B here + `make e2e` (1 A, 2 B) + the N=10 load (10 A).
#   1  make check (VERIFY_SKIP_CHECK=1 skips)
#   2  starting state: current version = Build ID, aws-agentcore compute config, nothing polling,
#      guardrails (budget + session alarm) exist
#   3  Agent Card in the Web UI (endpoint description, headless Chrome screenshot) and Q9 (what the
#      UI shows for the compute config: UI HTTP API + screenshot of the deployment page)
#   4  Case B fully in the cloud (scripts/demo/case-b.sh, eks-pf): input-required -> session drained
#      -> 1 pending Nexus op -> approve -> SECOND InvokeWorker caused by the Nexus completion (Q15)
#      -> session #2 replies with the refund ID; cold start of session #2; merged timeline saved
#   5  issue_refund idempotent on EKS (scripts/refund-idempotency.sh)
#   6  reject variant: apology, 2 sessions
#   7  make e2e TEMPORAL_PROFILE=eks-pf (Case A 1 session, Case B 2; VERIFY_SKIP_E2E=1 skips)
#   8  N=10 load (scripts/demo/load.sh): all complete, no WCI failures, sessions drain
#      (VERIFY_SKIP_LOAD=1 skips). N=50 is deferred to v1.2.
#   9  re-invoke cadence after a terminate with a reachable Worker (ledger 44)
#   10 AWS Q8: Sessions / ActiveSessionCount values for this run, alarm state
#   11 back to zero: no new session of ours for 5 min, no pollers, no local processes
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
exec > >(scripts/redact.sh) 2>&1
export AWS_REGION=us-east-1 AWS_PAGER=""
unset AWS_PROFILE
export KUBECONFIG="$PWD/.temporal/kubeconfig-eks"
export TEMPORAL_CONFIG_FILE="$PWD/config/temporal.toml" TEMPORAL_PROFILE=eks-pf
export SUPPORT_TASK_QUEUE=support-tq
LOGS=.temporal/verify-v0.13
rm -rf "$LOGS"; mkdir -p "$LOGS"
fail=0
step() { printf '\n=== %s  (%s)\n' "$*" "$(date -u +%H:%M:%SZ)"; }
ok()  { printf '  ok    %s\n' "$*"; }
bad() { printf '  FAIL  %s\n' "$*"; fail=1; }
check() { local what="$1"; shift; if "$@"; then ok "$what"; else bad "$what"; fi; }
UV=(uv run --frozen)
NS=agents DEP=support-supervisor TQ=support-tq
PF_STARTED=0
cleanup() { if [[ "$PF_STARTED" == 1 ]]; then scripts/eks.sh pf-stop >/dev/null 2>&1 || true; fi; }
trap cleanup EXIT
T_START="$(date +%s)"
pollers() {
  temporal task-queue describe --namespace $NS --task-queue $TQ -o json 2>/dev/null \
    | jq '[.. | objects | select(has("pollers")) | .pollers[]?] | length' 2>/dev/null || echo 0
}
sessions_of() { "${UV[@]}" python -m demo_testing.agentcore_logs sessions "$1" --since "$2" --build "$BUILD" --json; }

# --- 1 ---------------------------------------------------------------------------------
step "1. make check"
if [[ "${VERIFY_SKIP_CHECK:-0}" == 1 ]]; then echo "  skipped (VERIFY_SKIP_CHECK=1)"
else check "make check" bash -c "make check >$LOGS/check.log 2>&1"; fi

# --- 2 ---------------------------------------------------------------------------------
if ! nc -z 127.0.0.1 7233 2>/dev/null; then scripts/eks.sh pf >/dev/null; PF_STARTED=1; fi
step "2. Starting state"
BUILD="$(temporal worker deployment describe --namespace $NS --name $DEP -o json | jq -r .routingConfig.currentVersionBuildID)"
echo "  current version $DEP:$BUILD; HEAD $(git rev-parse --short HEAD); build-id.sh: $(scripts/build-id.sh 2>/dev/null || echo dirty)"
check "the registered version is the content hash at HEAD (no Support change in this tag)" \
  bash -c "[[ '$(scripts/build-id.sh 2>/dev/null)' == '$BUILD' ]]"
temporal worker deployment describe-version --namespace $NS --deployment-name $DEP --build-id "$BUILD" -o json >$LOGS/version.json
check "compute config aws-agentcore, scaler no-sync" bash -c \
  "jq -e '.computeConfig.scalingGroups.default.provider.type==\"aws-agentcore\" and .computeConfig.scalingGroups.default.scaler.type==\"no-sync\"' $LOGS/version.json >/dev/null"
"${UV[@]}" python -m demo_testing.agentcore_logs wait-idle --since "$(( $(date +%s) - 900 ))" --build "$BUILD" --timeout 300 | tail -1 | sed 's/^/  /'
ok "no AgentCore session of ours is polling"
aws budgets describe-budgets --account-id "$(aws sts get-caller-identity --query Account --output text)" \
  --query "Budgets[?BudgetName=='agentcore-demo-monthly'].{name: BudgetName, limit: BudgetLimit.Amount}" --output json | jq -c '.[]' | sed 's/^/  budget /'
alarm="$(aws cloudwatch describe-alarms --alarm-names agentcore-demo-active-sessions --query 'MetricAlarms[0].{ns: Namespace, metric: MetricName, dims: Dimensions, threshold: Threshold, state: StateValue}' --output json)"
echo "  alarm $(jq -c . <<<"$alarm")"
check "Budgets alert and session alarm exist (since v0.8)" bash -c "jq -e '.metric==\"ActiveSessionCount\" and .ns==\"AWS/Bedrock-AgentCore\"' <<<'$alarm' >/dev/null"

# --- 3 ---------------------------------------------------------------------------------
step "3. Agent Card in the Web UI; Q9 compute config in the UI"
eid="$(temporal operator nexus endpoint get --name finance-agent -o json | jq -r .id)"
curl -sf "http://127.0.0.1:8233/api/v1/nexus/endpoints/$eid" >$LOGS/ui-endpoint.json
jq -r '.endpoint.spec.description.data | @base64d | fromjson | split("\n")[0]' $LOGS/ui-endpoint.json | sed 's/^/  UI API description: /'
check "the UI serves the finance-agent description = the Agent Card" bash -c \
  "jq -r '.endpoint.spec.description.data | @base64d' $LOGS/ui-endpoint.json | grep -q 'Finance Refund Agent (A2A)'"
if scripts/ui-screenshot.sh "/nexus/$eid" $LOGS/ui-agent-card.png 2200 >/dev/null 2>&1; then
  ok "screenshot of the endpoint page: $LOGS/ui-agent-card.png (rendered Agent Card under 'Description')"
else echo "  (no Chrome: screenshot skipped)"; fi
curl -sf "http://127.0.0.1:8233/api/v1/namespaces/$NS/worker-deployment-versions/$DEP/$BUILD" \
  | jq -c '{provider: .workerDeploymentVersionInfo.computeConfig.scalingGroups.default.provider.type, scaler: .workerDeploymentVersionInfo.computeConfig.scalingGroups.default.scaler.type}' \
  | sed 's/^/  UI API compute config: /'
scripts/ui-screenshot.sh "/namespaces/$NS/worker-deployments/$DEP" $LOGS/ui-deployment.png 900 >/dev/null 2>&1 \
  && echo "  screenshot of the deployment page: $LOGS/ui-deployment.png (Q9: the Compute column)"

# --- 4 ---------------------------------------------------------------------------------
step "4. Case B fully in the cloud: drain while the human decides, approve, second invocation (Q15)"
WF="support-2077-v013-$(date +%s)"; T4="$(date +%s)"
CASE_B_APPROVE=approve COMMENT="photos checked" scripts/demo/case-b.sh "$WF" >$LOGS/case-b.log 2>&1 || true
grep -E 'input-required|drained|pendingNexus|approve sent|is now|Status|reply in' $LOGS/case-b.log | cut -c1-200 | sed 's/^/  /'
check "Finance reached input-required and the session drained before the approval" bash -c \
  "grep -q ': input-required' $LOGS/case-b.log && grep -q 'drained: no Support Worker' $LOGS/case-b.log"
check "describe while waiting: 1 pending Nexus operation (send_message STARTED)" bash -c \
  "grep -q '\"pendingNexusOperations\":\[{\"endpoint\":\"finance-agent\",\"operation\":\"send_message\",\"state\":\"PENDING_NEXUS_OPERATION_STATE_STARTED\"}\]' $LOGS/case-b.log"
refund="$(scripts/demo/task-state.sh "$WF" | jq -r '.artifacts[0].data.refund_id')"
check "Case B completed and the reply quotes Finance's refund ID $refund" bash -c \
  "grep -q 'Status          COMPLETED' $LOGS/case-b.log && grep -q '$refund' $LOGS/case-b.log"
"${UV[@]}" python -m demo_testing.agentcore_logs wait-drained "$WF" --since "$((T4 - 5))" --build "$BUILD" --timeout 300 >$LOGS/case-b-sessions.txt || true
sed 's/^/  /' $LOGS/case-b-sessions.txt
check "Case B ran in exactly 2 AgentCore sessions, both drained" grep -q '^sessions: 2$' $LOGS/case-b-sessions.txt
make -s timeline WF="$WF" MERGED=1 >$LOGS/timeline.txt 2>&1 || true
sed -n '/== One timeline/,$p' $LOGS/timeline.txt | grep -E 'NEXUS send_message|signal|wci|session #2|Q15|\+[0-9.]+s$|not seen' | sed 's/^/  /'
check "WCI: task-add-signal and InvokeWorker right after the Nexus completion (Q15)" bash -c \
  "grep -E '^ +task-add-signal +\+0\.' $LOGS/timeline.txt >/dev/null && grep -E '^ +InvokeWorker +\+0\.' $LOGS/timeline.txt >/dev/null"
check "session #2 started and ran the next Workflow Task (cold start recorded above)" bash -c \
  "grep -qE '^ +first Workflow Task +\+[0-9]' $LOGS/timeline.txt && grep -q 'session #2: process start' $LOGS/timeline.txt"
closed="$(temporal workflow describe --namespace $NS --workflow-id "$WF" -o json | jq -r .workflowExecutionInfo.closeTime)"
scripts/wci-history.sh "$BUILD" --since "$(date -u -r "$T4" +%FT%TZ)" >$LOGS/case-b-wci.log
inv="$(awk -v c="${closed:0:26}" '/activity  InvokeWorker$/ && substr($1,1,26) <= c' $LOGS/case-b-wci.log | wc -l | tr -d ' ')"
# Exactly 2 sessions ran the Workflow (checked above). The WCI may add one more InvokeWorker
# right after a PullStats that still reports the backlog the just-started session #2 has not
# taken yet: that session idles 30 s and runs nothing for the Workflow (ledger 73, seen by the
# v0.13 independent verification). So: at least 2, extras reported.
check "WCI InvokeWorker between start and close of Case B: $inv (2 expected; extras = ledger 73 idle sessions)" [ "$inv" -ge 2 ]
(( inv > 2 )) && echo "  note  $((inv - 2)) extra InvokeWorker (ledger 73): its session ran nothing for $WF"

# --- 5 ---------------------------------------------------------------------------------
step "5. issue_refund is idempotent (payments-mock on EKS, same Idempotency-Key)"
check "replaying Finance's refund call returns the same refund, still one refund for the key" \
  bash -c "scripts/refund-idempotency.sh '$WF' | tee $LOGS/idempotency.log | sed 's/^/  /'; grep -q 'idempotent: yes' $LOGS/idempotency.log"

# --- 6 ---------------------------------------------------------------------------------
step "6. Reject variant: the reply apologises"
WFR="support-2077-v013r-$(date +%s)"; T6="$(date +%s)"
CASE_B_APPROVE=reject COMMENT="duplicate claim" scripts/demo/case-b.sh "$WFR" >$LOGS/case-b-reject.log 2>&1 || true
grep -E 'reject sent|is now|Status|Result|reply in' $LOGS/case-b-reject.log | cut -c1-260 | sed 's/^/  /'
check "rejected task, completed Workflow, the reply apologises and has no refund ID" bash -c \
  "grep -q 'is now: rejected' $LOGS/case-b-reject.log && grep -q 'Status          COMPLETED' $LOGS/case-b-reject.log && grep -iqE 'sorry|apolog' $LOGS/case-b-reject.log && ! grep -qE 'R-1[0-9]{4}' $LOGS/case-b-reject.log"
"${UV[@]}" python -m demo_testing.agentcore_logs wait-drained "$WFR" --since "$((T6 - 5))" --build "$BUILD" --timeout 300 | tail -1 | sed 's/^/  /' | tee $LOGS/reject-sessions.txt
check "reject variant: 2 sessions as well" grep -q 'sessions: 2' $LOGS/reject-sessions.txt

# --- 7 ---------------------------------------------------------------------------------
step "7. make e2e TEMPORAL_PROFILE=eks-pf (WCI invocation counts: Case A 1, Case B 2)"
if [[ "${VERIFY_SKIP_E2E:-0}" == 1 ]]; then echo "  skipped (VERIFY_SKIP_E2E=1)"
else
  make e2e TEMPORAL_PROFILE=eks-pf PYTEST="uv run --frozen pytest -s" >$LOGS/e2e.log 2>&1 && e2e=0 || e2e=1
  grep -E 'AgentCore sessions|passed|failed' $LOGS/e2e.log | sed 's/^/  /'
  check "make e2e TEMPORAL_PROFILE=eks-pf green" [ "$e2e" == 0 ]
fi

# --- 8 ---------------------------------------------------------------------------------
step "8. Load: N=10 Case A at once (runbook N=50 deferred to v1.2)"
if [[ "${VERIFY_SKIP_LOAD:-0}" == 1 ]]; then echo "  skipped (VERIFY_SKIP_LOAD=1)"
else
  LOAD_OUT=$LOGS/load scripts/demo/load.sh "${VERIFY_LOAD_N:-10}" >$LOGS/load.log 2>&1 && load=0 || load=1
  sed 's/^/  /' $LOGS/load.log
  check "N=${VERIFY_LOAD_N:-10}: all completed, no failed WCI activity, every session drained" [ "$load" == 0 ]
fi

# --- 9 ---------------------------------------------------------------------------------
step "9. Re-invoke cadence after a terminate, Worker reachable (ledger 44; v0.8 saw 5 more invokes)"
"${UV[@]}" python -m demo_testing.agentcore_logs wait-idle --since "$(( $(date +%s) - 900 ))" --build "$BUILD" --timeout 300 >/dev/null || true
PR="reinvoke-$(date +%s)"; T9="$(date -u +%FT%TZ)"
temporal workflow start --namespace $NS --task-queue $TQ --type PingWorkflow --workflow-id "$PR" --input "\"$PR\"" >/dev/null
temporal workflow terminate --namespace $NS --workflow-id "$PR" --reason "v0.13 re-invoke probe" >/dev/null
echo "  started and terminated $PR at $T9 (before any session could poll); watching the WCI for 6 min"
sleep "${VERIFY_REINVOKE_WATCH:-360}"
scripts/wci-history.sh "$BUILD" --since "$T9" >$LOGS/reinvoke-wci.log 2>&1 || true
sed 's/^/  /' $LOGS/reinvoke-wci.log | tail -12
rinv="$(grep -c 'activity  InvokeWorker$' $LOGS/reinvoke-wci.log || true)"
temporal task-queue describe --namespace $NS --task-queue $TQ --report-stats -o json 2>/dev/null \
  | jq -c '[.. | objects | select(has("approximateBacklogCount")) | .approximateBacklogCount | tonumber? // .] ' 2>/dev/null | sed 's/^/  backlog counts: /' || true
check "after the terminate: $rinv InvokeWorker in 6 min (one for the task; no re-invoke storm)" [ "$rinv" -le 1 ]

# --- 10 --------------------------------------------------------------------------------
step "10. AWS Q8: session metrics for this run, alarm"
mins=$(( ( $(date +%s) - T_START ) / 60 + 2 ))
scripts/agentcore-sessions.sh --window "$mins" >$LOGS/q8.log 2>&1 || true
sed 's/^/  /' $LOGS/q8.log
check "Sessions{Name=<runtime>::b_${BUILD}} counted this run's sessions (> 0)" bash -c "grep -q '^sessions: [1-9]' $LOGS/q8.log"
st="$(aws cloudwatch describe-alarms --alarm-names agentcore-demo-active-sessions --query 'MetricAlarms[0].StateValue' --output text)"
echo "  alarm agentcore-demo-active-sessions: $st"
check "session alarm not in ALARM" [ "$st" != ALARM ]

# --- 11 --------------------------------------------------------------------------------
step "11. Back to zero"
n=1; for _ in $(seq 1 "${VERIFY_SESSION_TRIES:-16}"); do
  if scripts/agentcore-sessions.sh --window 5 >$LOGS/sessions.log 2>&1; then n=0; break; fi
  sleep 60
done
sed 's/^/  /' $LOGS/sessions.log
check "no new session of ours in the last 5 min (idle 120 s: every session has ended)" [ "$n" == 0 ]
p=1; for _ in $(seq 1 30); do [[ "$(pollers)" == 0 ]] && { p=0; break; }; sleep 20; done
check "zero pollers on support-tq" [ "$p" == 0 ]
check "no laptop Worker running" bash -c "! pgrep -f 'support_agent\.(worker|agentcore_app)' >/dev/null"
running="$(temporal workflow list --namespace $NS --query 'ExecutionStatus="Running"' -o json | jq length)"
check "no Support Workflow left running ($running)" [ "$running" == 0 ]

echo
if [[ $fail -eq 0 ]]; then echo "verify: OK"; else echo "verify: FAILED"; exit 1; fi
