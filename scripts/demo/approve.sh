#!/usr/bin/env bash
# The Finance approver answers an A2A task that is waiting in input-required (v0.6).
#   scripts/demo/approve.sh [support-workflow-id|a2a-task-id] [approver] [comment]
#   make approve WF=support-2077 APPROVER=dana.finance [COMMENT="photos checked"]
#   make reject  WF=support-2077 APPROVER=dana.finance [COMMENT="duplicate claim"]
# Sends the signal `approve` (or `reject`, when called as reject.sh or with
# VERDICT=reject) to a2a-task-<support workflow id> in namespace agent-services, with the
# input {"approver": ..., "comment": ...} (finance_agent.policy.ApproverDecision).
# Signals can't answer the sender, and the Workflow ignores a decision that arrives
# outside input-required, so this script checks the state with get_task first and
# refuses otherwise. Honours TEMPORAL_PROFILE / TEMPORAL_CONFIG_FILE (envconfig).
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
export TEMPORAL_CONFIG_FILE="${TEMPORAL_CONFIG_FILE:-$PWD/config/temporal.toml}"
export TEMPORAL_PROFILE="${TEMPORAL_PROFILE:-local}"

case "$(basename "$0")" in reject.sh) DEFAULT_VERDICT=reject ;; *) DEFAULT_VERDICT=approve ;; esac
VERDICT="${VERDICT:-$DEFAULT_VERDICT}"
case "$VERDICT" in approve|reject) ;; *) echo "VERDICT must be approve or reject, got '$VERDICT'" >&2; exit 2 ;; esac
WF="${1:-support-2077}"
APPROVER="${2:-${APPROVER:-dana.finance}}"
COMMENT="${3:-${COMMENT:-}}"
TASK="$WF"; [[ "$TASK" == a2a-task-* ]] || TASK="a2a-task-$WF"

state() {
  temporal workflow query --namespace agent-services --workflow-id "$TASK" --type get_task -o json \
    | jq -r '.queryResult[0].status.state'
}

now="$(state)"
if [[ "$now" != "input-required" ]]; then
  echo "$TASK is '$now', not 'input-required': nothing to $VERDICT" >&2
  exit 1
fi
temporal workflow signal --namespace agent-services --workflow-id "$TASK" --name "$VERDICT" \
  --input "$(jq -cn --arg a "$APPROVER" --arg c "$COMMENT" '{approver: $a, comment: $c}')"
echo "$VERDICT sent to $TASK as $APPROVER"
for _ in $(seq 1 50); do
  now="$(state)"; [[ "$now" == "input-required" ]] || break; sleep 0.2
done
echo "$TASK is now: $now"
