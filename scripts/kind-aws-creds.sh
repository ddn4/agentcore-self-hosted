#!/usr/bin/env bash
# Short-lived AWS credentials for the Finance pods on kind (plan 3.2; no IRSA on kind):
# `aws configure export-credentials` (the default SSO profile) -> Secret
# temporal-workers/aws-creds, read by overlays/kind via envFrom (optional). Then
# `make kind-workers FINANCE_MODEL=bedrock`. Expires with the SSO session; never committed.
#   scripts/kind-aws-creds.sh        (make kind-aws-creds)
set -euo pipefail
exec "$(git rev-parse --show-toplevel)/scripts/kind.sh" aws-creds
