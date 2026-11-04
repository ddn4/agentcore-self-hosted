#!/usr/bin/env bash
# Copy client certificates from the cluster to .certs/ (gitignored) for laptop clients
# (plan v0.9: Support's certificate, default no-AWS path = local files + _PATH vars).
#   scripts/certs-pull.sh                 (make certs-pull)    kind -> .certs/
# Writes .certs/ca.crt (the root, from any issued Secret's ca.crt) and, per identity,
# .certs/<name>/{tls.crt,tls.key,ca.crt} (keys 0600):
#   operator          CLI and demo scripts (profile kind)
#   agentcore-worker  the AgentCore-shaped Support Worker (make agentcore-local*, TEMPORAL_PROFILE=kind)
#   laptop-support    the unversioned laptop Support Worker (make worker-support*)
# Re-run after a renewal: the SDK reads the files at connect time (ledger 33, Q18).
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
export KUBECONFIG="$PWD/.temporal/kubeconfig-agentcore-demo"
OUT=.certs
umask 077
mkdir -p "$OUT"
pull() {  # pull SECRET DIR
  local json; json="$(kubectl get secret "$1" -n temporal -o json)"
  mkdir -p "$OUT/$2"
  for f in tls.crt tls.key ca.crt; do
    jq -r --arg f "$f" '.data[$f]' <<<"$json" | base64 -d >"$OUT/$2/$f"
  done
  printf '  %-17s CN=%s, expires %s\n' "$2" \
    "$(openssl x509 -in "$OUT/$2/tls.crt" -noout -subject -nameopt multiline | sed -n 's/ *commonName *= //p')" \
    "$(openssl x509 -in "$OUT/$2/tls.crt" -noout -enddate | cut -d= -f2)"
}
echo "certs-pull: kind -> $OUT/"
pull temporal-operator-tls operator
pull agentcore-worker-tls agentcore-worker
pull laptop-support-tls laptop-support
cp "$OUT/operator/ca.crt" "$OUT/ca.crt"
echo "  ca.crt            $(openssl x509 -in "$OUT/ca.crt" -noout -subject -nameopt oneline)"
