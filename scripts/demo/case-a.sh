#!/usr/bin/env bash
# Case A: the Support Supervisor answers ticket #1042 (headphones, $40, damaged).
#   scripts/demo/case-a.sh [workflow-id]          (make case-a [WF=support-1042])
# v0.3: it asks its Order Lookup and Return Policy sub-agents first (agents as tools).
# v0.5: it discovers the Finance agent's Agent Card and asks Finance for the refund over
#       Nexus (endpoint finance-agent -> namespace agent-services). SUPPORT_A2A_MODE picks
#       how: tool (default; ask_finance_agent in-workflow tool) or handoff (runbook fallback).
# v0.6: the third Workflow input is SUPPORT_NEXUS_SEND_TIMEOUT (default 25h; ADR-0009), the
#       budget for Finance's answer, which may include a human approval.
# Starts SupportSupervisorWorkflow in namespace agents (or attaches to the open run
# with the same ID: --id-conflict-policy UseExisting) and waits for its result
# (CASE_A_WAIT=0: start only; case-b.sh does that).
# Needs: make worker-support (or worker-support-scripted), make worker-finance (or
# worker-finance-scripted), make payments-mock, make nexus-endpoint. Crash demo: Ctrl-C the
# Support worker during an invoke_model Activity, start it again; this still prints the
# answer. Honours TEMPORAL_PROFILE / TEMPORAL_CONFIG_FILE (envconfig, like the SDK).
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
export TEMPORAL_CONFIG_FILE="${TEMPORAL_CONFIG_FILE:-$PWD/config/temporal.toml}"
export TEMPORAL_PROFILE="${TEMPORAL_PROFILE:-local}"

WF="${1:-support-1042}"
TQ="${SUPPORT_TASK_QUEUE:-support-dev-tq}"
MODE="${SUPPORT_A2A_MODE:-tool}"
BUDGET="${SUPPORT_NEXUS_SEND_TIMEOUT:-25h}"
TICKET="${CASE_A_TICKET:-Order #1042, my headphones arrived broken. I want my money back.}"
case "$MODE" in tool|handoff) ;; *) echo "SUPPORT_A2A_MODE must be tool or handoff, got '$MODE'" >&2; exit 2 ;; esac

temporal workflow start --namespace agents --task-queue "$TQ" \
  --type SupportSupervisorWorkflow --workflow-id "$WF" \
  --id-conflict-policy UseExisting \
  --input "$(jq -cn --arg t "$TICKET" '$t')" --input "$(jq -cn --arg m "$MODE" '$m')" \
  --input "$(jq -cn --arg b "$BUDGET" '$b')"
UI="http://127.0.0.1:8233/namespaces/agents/workflows/$WF"
if [[ "${CASE_A_WAIT:-1}" == "0" ]]; then
  echo "started $WF (A2A mode: $MODE, Nexus budget: $BUDGET; UI: $UI)"
  exit 0
fi
echo "waiting for $WF (A2A mode: $MODE, Nexus budget: $BUDGET; UI: $UI)"
temporal workflow result --namespace agents --workflow-id "$WF"
