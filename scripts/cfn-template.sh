#!/usr/bin/env bash
# Vendor Temporal's self-hosted AgentCore invocation-role template (runbook 6.2).
#   scripts/cfn-template.sh            (make cfn-template) download, check sha256, vendor
#   scripts/cfn-template.sh --check    offline: the vendored file still matches the pin
# Source: the "Download the CloudFormation template" link on
#   https://docs.temporal.io/production-deployment/worker-deployments/serverless-workers/agentcore/self-hosted-setup
# (the hash in the file name changes with the template; the old /files/... URL is a 404).
# NOT the Cloud template or the sample's mk-invoke-role.sh: both trust Temporal Cloud.
# Verified parameters (v0.8): TemporalIamRoleArn, AssumeRoleExternalId (5-45,
# [a-zA-Z0-9_+=,.@-]*), AgentRuntimeARNs (CommaDelimitedList), RoleName (default
# Temporal-AgentCore-Worker); outputs RoleARN, AgentRuntimeARNs.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
URL="https://docs.temporal.io/assets/files/temporal-self-hosted-serverless-worker-agentcore-role-d633a078183a821913145e7fb41bb8cb.yaml"
SHA256=033ff21ba09a56d31d611f661a65782837b0cfb30f74698bb84efd6999a1d4e3
OUT=deploy/agentcore/temporal-self-hosted-serverless-worker-agentcore-role.yaml

check() { echo "$SHA256  $1" | shasum -a 256 -c - >/dev/null; }

if [[ "${1:-}" == --check ]]; then
  check "$OUT" && echo "cfn-template: $OUT matches sha256 $SHA256" && exit 0
  echo "cfn-template: $OUT does not match the pinned sha256" >&2; exit 1
fi

tmp="$(mktemp)"; trap 'rm -f "$tmp"' EXIT
curl -sSfL -o "$tmp" "$URL"
if ! check "$tmp"; then
  echo "cfn-template: downloaded template changed (sha256 $(shasum -a 256 "$tmp" | cut -d' ' -f1))." >&2
  echo "  Review it (parameters, trust policy), then update SHA256 here and the README." >&2
  exit 1
fi
for p in TemporalIamRoleArn AssumeRoleExternalId AgentRuntimeARNs RoleName; do
  grep -q "^  $p:" "$tmp" || { echo "cfn-template: parameter $p missing" >&2; exit 1; }
done
mkdir -p "$(dirname "$OUT")"
cp "$tmp" "$OUT"
echo "cfn-template: vendored $OUT (sha256 $SHA256; parameters TemporalIamRoleArn, AssumeRoleExternalId, AgentRuntimeARNs, RoleName)"
