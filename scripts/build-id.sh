#!/usr/bin/env bash
# Support's Build ID: a content hash (plan 3.3, ADR-0004), not a commit SHA.
#   scripts/build-id.sh            -> 7 hex chars, e.g. 3f9c2a1
# Hashes exactly what the AgentCore bundle runs: the committed trees of
# services/support-agent and packages/finance-a2a-api, plus Support's exported
# dependency set. Docs, infra and Finance-only commits (and squash-merges) leave it
# unchanged. The git SHA travels separately as SUPPORT_GIT_SHA (metadata only).
# Refuses when any input is dirty: a Build ID must name code that exists in a commit.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

# uv.lock feeds the exported dependency set, so it must be clean too.
inputs=(services/support-agent packages/finance-a2a-api uv.lock)
dirty="$(git status --porcelain --untracked-files=all -- "${inputs[@]}")"
if [[ -n "$dirty" ]]; then
  echo "build-id: refusing: uncommitted changes in the Build ID inputs:" >&2
  printf '%s\n' "$dirty" | sed 's/^/  /' >&2
  echo "commit them (or set TEMPORAL_BUILD_ID explicitly for a throwaway local run)" >&2
  exit 1
fi

{
  git rev-parse HEAD:services/support-agent
  git rev-parse HEAD:packages/finance-a2a-api
  uv export --package support-agent --frozen --no-dev --no-hashes 2>/dev/null | shasum
} | shasum | cut -c1-7
