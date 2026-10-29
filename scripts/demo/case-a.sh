#!/usr/bin/env bash
# Case A: the Support Supervisor answers ticket #1042 (headphones, $40, damaged).
# v0.3: it asks its Order Lookup and Return Policy sub-agents first (agents as tools).
#   scripts/demo/case-a.sh [workflow-id]          (make case-a [WF=support-1042])
# Starts SupportSupervisorWorkflow in namespace agents (or attaches to the open run
# with the same ID: --id-conflict-policy UseExisting) and waits for its result.
# Run `make worker-support` in another terminal. Crash demo: Ctrl-C the worker while
# an invoke_model Activity is running, start it again, and this command still prints
# the answer. Honours TEMPORAL_PROFILE / TEMPORAL_CONFIG_FILE (envconfig, like the SDK).
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
export TEMPORAL_CONFIG_FILE="${TEMPORAL_CONFIG_FILE:-$PWD/config/temporal.toml}"
export TEMPORAL_PROFILE="${TEMPORAL_PROFILE:-local}"

WF="${1:-support-1042}"
TQ="${SUPPORT_TASK_QUEUE:-support-dev-tq}"
TICKET="${CASE_A_TICKET:-Order #1042, my headphones arrived broken. I want my money back.}"

temporal workflow start --namespace agents --task-queue "$TQ" \
  --type SupportSupervisorWorkflow --workflow-id "$WF" \
  --id-conflict-policy UseExisting \
  --input "$(jq -cn --arg t "$TICKET" '$t')"
echo "waiting for $WF (UI: http://127.0.0.1:8233/namespaces/agents/workflows/$WF)"
temporal workflow result --namespace agents --workflow-id "$WF"
