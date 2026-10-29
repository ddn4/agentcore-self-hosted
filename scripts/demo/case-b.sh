#!/usr/bin/env bash
# Case B: the Support Supervisor answers ticket #2077 (laptop, $450, damaged).
#   scripts/demo/case-b.sh [workflow-id]          (make case-b [WF=support-2077])
# v0.3: the Return Policy sub-agent finds the refund is over $100, so the reply
# says the request is escalated for Finance manager approval. (v0.5 sends it to
# the Finance agent over Nexus; v0.6 adds the human approval.)
# Starts SupportSupervisorWorkflow in namespace agents (or attaches to the open run
# with the same ID) and waits for its result. Run `make worker-support` (or
# `make worker-support-scripted`) in another terminal. Honours TEMPORAL_PROFILE.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
CASE_A_TICKET="${CASE_B_TICKET:-Order #2077: my laptop arrived with a cracked screen. I want a refund.}" \
  exec scripts/demo/case-a.sh "${1:-support-2077}"
