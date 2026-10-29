#!/usr/bin/env bash
# Create (or update) the Nexus endpoint `finance-agent` -> agent-services/agent-services-tq,
# with the Finance Agent Card as its description (runbook 7b.8). Idempotent.
#   scripts/nexus-endpoint.sh            create or update  (make nexus-endpoint)
#   scripts/nexus-endpoint.sh show       print the endpoint
#   scripts/nexus-endpoint.sh delete     delete it
# Connection: TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE (envconfig), as everywhere.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
export TEMPORAL_CONFIG_FILE="${TEMPORAL_CONFIG_FILE:-$PWD/config/temporal.toml}"
export TEMPORAL_PROFILE="${TEMPORAL_PROFILE:-local}"

NAME="${NEXUS_ENDPOINT:-finance-agent}"
TARGET_NS="${FINANCE_NAMESPACE:-agent-services}"
TARGET_TQ="${FINANCE_TASK_QUEUE:-agent-services-tq}"
CARD=.generated/finance-agent-card.md

exists() { temporal operator nexus endpoint get --name "$NAME" >/dev/null 2>&1; }

case "${1:-apply}" in
  apply)
    uv run --frozen python scripts/render-agent-card.py --out "$CARD" >/dev/null
    if exists; then
      temporal operator nexus endpoint update --name "$NAME" \
        --target-namespace "$TARGET_NS" --target-task-queue "$TARGET_TQ" \
        --description-file "$CARD"
      echo "nexus-endpoint: updated $NAME -> $TARGET_NS/$TARGET_TQ (description: $CARD)"
    else
      temporal operator nexus endpoint create --name "$NAME" \
        --target-namespace "$TARGET_NS" --target-task-queue "$TARGET_TQ" \
        --description-file "$CARD"
      echo "nexus-endpoint: created $NAME -> $TARGET_NS/$TARGET_TQ (description: $CARD)"
    fi
    ;;
  show)
    temporal operator nexus endpoint get --name "$NAME"
    ;;
  delete)
    if exists; then temporal operator nexus endpoint delete --name "$NAME"; else echo "nexus-endpoint: $NAME not found"; fi
    ;;
  *)
    echo "usage: $0 [apply|show|delete]" >&2; exit 2 ;;
esac
