#!/usr/bin/env bash
# Case B: the Support Supervisor answers ticket #2077 (laptop, $450, damaged).
#   scripts/demo/case-b.sh [workflow-id]          (make case-b [WF=support-2077])
# v0.3: the Return Policy sub-agent finds the refund is over $100 (Finance manager approval).
# v0.5: the Supervisor sends it to the Finance agent over Nexus; Finance's auto-approval
#       limit is $100, so the A2A task ends `rejected` and the reply says it is escalated.
#       (v0.6 adds input-required and the human approval.)
# Same as case-a.sh with another ticket; honours SUPPORT_A2A_MODE and TEMPORAL_PROFILE.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
CASE_A_TICKET="${CASE_B_TICKET:-Order #2077: my laptop arrived with a cracked screen. I want a refund.}" \
  exec scripts/demo/case-a.sh "${1:-support-2077}"
