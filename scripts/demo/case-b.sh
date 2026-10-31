#!/usr/bin/env bash
# Case B: the Support Supervisor answers ticket #2077 (laptop, $450, damaged).
#   scripts/demo/case-b.sh [workflow-id]          (make case-b [WF=support-2077])
# v0.3: the Return Policy sub-agent finds the refund is over $100 (Finance manager approval).
# v0.5: the Supervisor sends it to the Finance agent over Nexus.
# v0.6: $450 is over Finance's $100 auto-approval limit, so the A2A task goes to
#       input-required and waits (up to FINANCE_APPROVAL_TIMEOUT, default 24h) for a human:
#         make task-state WF=support-2077        # input-required
#         make pending WF=support-2077           # Support: one pending Nexus operation
#         make approve WF=support-2077 APPROVER=dana.finance   (or make reject)
#       Stop the Support worker while it waits if you like: nothing is lost.
#       By default this script starts the conversation and returns (CASE_B_WAIT=1 waits for
#       the reply, like case-a.sh). Then: make result WF=support-2077.
# v0.13 (TEMPORAL_PROFILE=eks-pf, or CASE_B_DRAIN_WAIT=1): Case B fully in the cloud. Support
#       runs on AgentCore (queue support-tq), Finance on EKS. After the start the script waits
#         1. for get_task = input-required (Finance is waiting for a human), then
#         2. for evidence in the AgentCore logs that every session that ran this conversation
#            has drained ("async task released", i.e. debounce 30 s after the last Activity),
#            plus CASE_B_DRAIN_MARGIN seconds (default 10),
#       shows Support's one pending Nexus operation, and then asks "approve now?" (on a
#       terminal) or does what CASE_B_APPROVE says: approve | reject | no (default: ask on a
#       terminal, no otherwise). After a decision it waits for the reply (session #2).
# Honours SUPPORT_A2A_MODE, SUPPORT_NEXUS_SEND_TIMEOUT, SUPPORT_TASK_QUEUE and TEMPORAL_PROFILE.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
export TEMPORAL_CONFIG_FILE="${TEMPORAL_CONFIG_FILE:-$PWD/config/temporal.toml}"
export TEMPORAL_PROFILE="${TEMPORAL_PROFILE:-local}"
WF="${1:-support-2077}"
TICKET="${CASE_B_TICKET:-Order #2077: my laptop arrived with a cracked screen. I want a refund.}"

DRAIN_WAIT="${CASE_B_DRAIN_WAIT:-0}"
if [[ "$TEMPORAL_PROFILE" == eks-pf ]]; then
  DRAIN_WAIT="${CASE_B_DRAIN_WAIT:-1}"
  # In the cloud the only Support Workers are AgentCore sessions on the versioned queue.
  [[ "${SUPPORT_TASK_QUEUE:-support-dev-tq}" == support-dev-tq ]] && export SUPPORT_TASK_QUEUE=support-tq
fi

if [[ "$DRAIN_WAIT" != 1 ]]; then
  CASE_A_WAIT="${CASE_B_WAIT:-0}" CASE_A_TICKET="$TICKET" exec scripts/demo/case-a.sh "$WF"
fi

UV=(uv run --frozen)
T0="$(date +%s)"
CASE_A_WAIT=0 CASE_A_TICKET="$TICKET" scripts/demo/case-a.sh "$WF"
ts() { date -u +%H:%M:%SZ; }

echo "[$(ts)] waiting for Finance's task to reach input-required"
state=""
for _ in $(seq 1 "${CASE_B_STATE_TRIES:-60}"); do
  state="$(scripts/demo/task-state.sh "$WF" 2>/dev/null | jq -r '.state' 2>/dev/null || true)"
  [[ "$state" == input-required || "$state" == completed || "$state" == rejected || "$state" == failed ]] && break
  sleep 5
done
[[ "$state" == input-required ]] || { echo "a2a-task-$WF is '${state:-absent}', not input-required" >&2; exit 1; }
echo "[$(ts)] a2a-task-$WF: input-required (Finance waits for a human approver)"

echo "[$(ts)] waiting for the AgentCore session(s) of $WF to drain (debounce + log delivery)"
"${UV[@]}" python -m demo_testing.agentcore_logs wait-drained "$WF" --since "$((T0 - 5))" \
  --timeout "${CASE_B_DRAIN_TIMEOUT:-300}"
sleep "${CASE_B_DRAIN_MARGIN:-10}"
echo "[$(ts)] drained: no Support Worker is running anywhere; the conversation lives in Temporal"
temporal workflow describe --namespace agents --workflow-id "$WF" -o json \
  | jq -c '{status: .workflowExecutionInfo.status, pendingNexusOperations: [(.pendingNexusOperations // [])[] | {endpoint, operation, state}]}'

decision="${CASE_B_APPROVE:-}"
if [[ -z "$decision" ]]; then
  if [[ -t 0 ]]; then
    read -r -p "approve now? [y = approve / r = reject / N = leave it waiting] " answer
    case "$answer" in y|Y) decision=approve ;; r|R) decision=reject ;; *) decision=no ;; esac
  else
    decision=no
  fi
fi
case "$decision" in
  approve|reject)
    T1="$(date +%s)"
    VERDICT="$decision" scripts/demo/approve.sh "$WF" "${APPROVER:-dana.finance}" "${COMMENT:-}"
    echo "[$(ts)] waiting for the reply (the Nexus completion makes the WCI start a new session)"
    temporal workflow result --namespace agents --workflow-id "$WF"
    echo "[$(ts)] reply in $(( $(date +%s) - T1 )) s after the $decision"
    ;;
  no) echo "left waiting: make approve WF=$WF (or make reject) when ready" ;;
  *) echo "CASE_B_APPROVE must be approve, reject or no, got '$decision'" >&2; exit 2 ;;
esac
