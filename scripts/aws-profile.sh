#!/usr/bin/env bash
# Append the [profile temporal-dev-wci] block to ~/.aws/config (plan v0.8 step 2). The
# laptop dev server's WCI runs with AWS_PROFILE=temporal-dev-wci: SSO credentials of the
# default profile, then sts:AssumeRole into temporal-dev-wci (Terraform 30-agentcore-iam).
#   scripts/aws-profile.sh [--backup PATH]       (make aws-profile)
# Append-only and idempotent: never edits another block; a backup is written first.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
CONFIG="${AWS_CONFIG_FILE:-$HOME/.aws/config}"
PROFILE=temporal-dev-wci
BACKUP="$CONFIG.pre-v08.bak"
[[ "${1:-}" == --backup ]] && BACKUP="$2"
if grep -q "^\[profile $PROFILE\]" "$CONFIG" 2>/dev/null; then
  echo "aws-profile: [profile $PROFILE] already in $CONFIG (unchanged)"; exit 0
fi
role="$(.tools/bin/terraform -chdir=infra/terraform/30-agentcore-iam output -raw dev_wci_role_arn)"
[[ "$role" == arn:aws:iam::*:role/temporal-dev-wci ]] || { echo "aws-profile: no dev_wci_role_arn (apply 30-agentcore-iam with create_dev_wci_role=true)" >&2; exit 1; }
cp -p "$CONFIG" "$BACKUP"
printf '\n[profile %s]\nsource_profile = default\nrole_arn       = %s\nregion         = us-east-1\n' "$PROFILE" "$role" >>"$CONFIG"
echo "aws-profile: appended [profile $PROFILE] to $CONFIG (backup $BACKUP)"
AWS_PROFILE=$PROFILE aws sts get-caller-identity --query Arn --output text | scripts/redact.sh
