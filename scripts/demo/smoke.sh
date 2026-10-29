#!/usr/bin/env bash
# v0.4 smoke: the A2A hop to Finance over Nexus with NO LLM on the caller side.
#   scripts/demo/smoke.sh [workflow-id] [amount] [order]   (make smoke [WF=smoke-1042 AMOUNT=40 ORDER=1042])
# Starts A2ASmokeWorkflow in namespace agents on nexus-smoke-tq (make worker-smoke), which calls
# FinanceAgentA2A.get_agent_card and .send_message through endpoint finance-agent; the Finance
# task runs in namespace agent-services as a2a-task-<workflow-id> (make worker-finance[-scripted]).
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
export TEMPORAL_CONFIG_FILE="${TEMPORAL_CONFIG_FILE:-$PWD/config/temporal.toml}"
export TEMPORAL_PROFILE="${TEMPORAL_PROFILE:-local}"

WF="${1:-smoke-1042}"
AMOUNT="${2:-40}"
ORDER="${3:-1042}"
TQ="${SMOKE_TASK_QUEUE:-nexus-smoke-tq}"

temporal workflow start --namespace agents --task-queue "$TQ" \
  --type A2ASmokeWorkflow --workflow-id "$WF" --id-conflict-policy UseExisting \
  --input "$AMOUNT" --input "$(jq -cn --arg o "$ORDER" '$o')"
echo "waiting for $WF (UI: http://127.0.0.1:8233/namespaces/agents/workflows/$WF)"
temporal workflow result --namespace agents --workflow-id "$WF" -o json | jq '.result // .'
