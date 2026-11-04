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
# Honours SUPPORT_A2A_MODE, SUPPORT_NEXUS_SEND_TIMEOUT, SUPPORT_TASK_QUEUE and TEMPORAL_PROFILE.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
export TEMPORAL_CONFIG_FILE="${TEMPORAL_CONFIG_FILE:-$PWD/config/temporal.toml}"
export TEMPORAL_PROFILE="${TEMPORAL_PROFILE:-local}"
WF="${1:-support-2077}"
TICKET="${CASE_B_TICKET:-Order #2077: my laptop arrived with a cracked screen. I want a refund.}"

CASE_A_WAIT="${CASE_B_WAIT:-0}" CASE_A_TICKET="$TICKET" exec scripts/demo/case-a.sh "$WF"
