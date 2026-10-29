#!/usr/bin/env bash
# The A2A task as Finance sees it: the get_task query on a2a-task-<support workflow id>
# in namespace agent-services (runbook §1.4 "Visible task state").
#   scripts/demo/task-state.sh [support-workflow-id]     (make task-state [WF=support-1042])
# Accepts the Support (caller) Workflow ID or the task ID itself (a2a-task-...).
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
export TEMPORAL_CONFIG_FILE="${TEMPORAL_CONFIG_FILE:-$PWD/config/temporal.toml}"
export TEMPORAL_PROFILE="${TEMPORAL_PROFILE:-local}"

WF="${1:-support-1042}"
TASK="$WF"; [[ "$TASK" == a2a-task-* ]] || TASK="a2a-task-$WF"
temporal workflow query --namespace agent-services --workflow-id "$TASK" --type get_task -o json \
  | jq '.queryResult[0] | {id, context_id, state: .status.state, message: .status.message.parts[0].text,
         artifacts: [.artifacts[] | {name, data: .parts[0].data}]}'
