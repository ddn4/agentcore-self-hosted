#!/usr/bin/env bash
# Failure demo (plan v0.11; runbook Phase 8 step 7.5): the Finance handler goes away mid-conversation.
#   scripts/demo/failure.sh [workflow-id]          (make failure TEMPORAL_PROFILE=kind [WF=support-1042-failure])
# 1. start Case A (order 1042) on Support's queue and wait until the A2A discovery
#    (get_agent_card, sync, 30 s budget) has completed: a Finance outage BEFORE discovery fails
#    the conversation outright (found at v0.11, ledger 68), which is not the demo
# 2. scale the kind Finance Deployment to 0 (no poller on agent-services-tq) while the
#    sub-agents run; the Supervisor then schedules send_message
# 3. watch the caller's pending Nexus operation retry (attempt count, last attempt failure);
#    the WCI does not help here: agent-services has no compute config (runbook 7.5)
# 4. scale Finance back to 2; the operation completes and Support replies
# Needs: the Support Worker running (e.g. make worker-support TEMPORAL_PROFILE=kind), the
# finance-agent endpoint, `make kind-workers` and `make pf`. FAILURE_MIN_ATTEMPTS (default 2) is how
# many failed attempts to show before scaling back; FAILURE_REPLICAS (default 2) the target.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
export TEMPORAL_CONFIG_FILE="${TEMPORAL_CONFIG_FILE:-$PWD/config/temporal.toml}"
export TEMPORAL_PROFILE="${TEMPORAL_PROFILE:-kind}"
WF="${1:-support-1042-failure-$(date +%s)}"
MIN="${FAILURE_MIN_ATTEMPTS:-2}"
REPLICAS="${FAILURE_REPLICAS:-2}"
export KUBECONFIG="$PWD/.temporal/kubeconfig-agentcore-demo"
k() { kubectl --context kind-agentcore-demo -n temporal-workers "$@"; }
say() { printf '[failure %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

pending() {
  temporal workflow describe --namespace agents --workflow-id "$WF" -o json \
    | jq -c '[(.pendingNexusOperations // [])[] | {operation, state, attempt: (.attempt // 0),
              lastAttemptFailure: ((.lastAttemptFailure.message // "") | .[0:120])}]'
}

discovered() {
  temporal workflow show --namespace agents --workflow-id "$WF" -o json \
    | jq -e '[.events[] | select(.eventType == "EVENT_TYPE_NEXUS_OPERATION_COMPLETED")] | length >= 1' >/dev/null
}

say "1. start Case A as $WF; wait for the A2A discovery (get_agent_card) to complete"
CASE_A_WAIT=0 scripts/demo/case-a.sh "$WF" >/dev/null
for _ in $(seq 1 60); do discovered && break; sleep 0.5; done
discovered || { say "get_agent_card did not complete"; exit 1; }

say "2. scale agent-services-worker to 0 (the sub-agents are still working)"
k scale deploy/agent-services-worker --replicas=0 >/dev/null
k wait --for=delete pod -l app=agent-services-worker --timeout=120s >/dev/null 2>&1 || true
say "   Finance pods: $(k get pods -l app=agent-services-worker --no-headers 2>/dev/null | wc -l | tr -d ' ')"

say "3. the A2A hop waits; Nexus retries are visible on the caller"
seen=""
for _ in $(seq 1 120); do
  p="$(pending)"
  sm="$(jq -r '[.[] | select(.operation == "send_message")][0] // empty' <<<"$p")"
  if [[ -n "$sm" ]]; then
    [[ "$sm" != "$seen" ]] && { say "   pending: $sm"; seen="$sm"; }
    att="$(jq -r .attempt <<<"$sm")"
    [[ "$att" -ge "$MIN" ]] && break
  fi
  sleep 5
done
[[ -n "$seen" ]] || { say "no pending send_message operation appeared (is the Support Worker running?)"; exit 1; }

say "4. scale agent-services-worker back to $REPLICAS"
k scale deploy/agent-services-worker --replicas="$REPLICAS" >/dev/null
k rollout status deploy/agent-services-worker --timeout=300s >/dev/null
say "   Finance pods ready: $(k get deploy agent-services-worker -o jsonpath='{.status.readyReplicas}')"
temporal workflow result --namespace agents --workflow-id "$WF" -o json | jq -r '.result // .' | head -c 400; echo
say "   $(scripts/demo/task-state.sh "$WF" | jq -c '{state, handled_by: .artifacts[0].data.handled_by, refund_id: .artifacts[0].data.refund_id}')"
say "recovered"
