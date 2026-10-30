#!/usr/bin/env bash
# The Finance approver rejects a refund waiting in input-required (v0.6).
#   scripts/demo/reject.sh [support-workflow-id|a2a-task-id] [approver] [comment]
#   make reject WF=support-2077 APPROVER=dana.finance [COMMENT="duplicate claim"]
# Same as approve.sh with the signal `reject`.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
VERDICT=reject exec scripts/demo/approve.sh "$@"
