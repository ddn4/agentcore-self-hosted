#!/usr/bin/env bash
# Force cert-manager to re-issue a Certificate now (rotation drill, plan v0.9).
#   scripts/cert-renew.sh NAMESPACE CERTIFICATE
# Uses `cmctl renew` when installed; otherwise does what cmctl does: sets the
# Certificate's Issuing condition to True through the status subresource.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
export KUBECONFIG="$PWD/.temporal/kubeconfig-agentcore-demo"
ns="${1:?namespace}" cert="${2:?certificate}"
if command -v cmctl >/dev/null; then exec cmctl renew -n "$ns" "$cert"; fi
now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
kubectl patch certificate "$cert" -n "$ns" --subresource=status --type=json -p "$(jq -cn --arg t "$now" '[
  {op: "add", path: "/status/conditions/-",
   value: {type: "Issuing", status: "True", reason: "ManuallyTriggered",
           message: "Certificate re-issuance manually triggered (scripts/cert-renew.sh)",
           lastTransitionTime: $t}}]')" >/dev/null
echo "cert-renew: $ns/$cert re-issuance triggered"
