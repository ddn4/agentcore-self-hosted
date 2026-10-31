#!/usr/bin/env bash
# Export the AgentCore Worker client cert (CN agentcore-worker) from the cluster to
# AWS Secrets Manager in the format support_agent.mtls reads: {"tls_crt","tls_key","ca_crt"}
# (runbook 2.6 step 5, 5.3).
#   scripts/export-agentcore-cert.sh --secret-id NAME [--cluster eks|kind] [--kms-key-id KEY] [--yes]
#   scripts/export-agentcore-cert.sh --secret-id NAME --check     (read-only: presence, keys, cert dates)
#   make export-agentcore-cert CLUSTER=eks SECRET_ID=temporal/agentcore-worker-mtls CONFIRM=yes
# EKS (v0.12): --cluster eks --secret-id temporal/agentcore-worker-mtls. The execution role may
#   read exactly temporal/agentcore-worker-mtls-* (30-agentcore-iam). Default encryption key
#   (aws/secretsmanager): the execution role needs no kms:Decrypt grant for it.
# kind (optional SM path at v0.9, not taken): --secret-id temporal-kind/agentcore-worker-mtls
#   (outside the execution role's pattern on purpose).
# CREATES OR UPDATES AN AWS RESOURCE: refuses without --yes (or EXPORT_AGENTCORE_CERT_CONFIRM=yes).
# create-secret the first time, put-secret-value on renewal (runbook 2.6 step 5: re-run after
# cert-manager renews; the cert is 4320h with renewBefore 720h). The PEMs never touch disk:
# they go from kubectl straight into the --secret-string argument. Afterwards the laptop's copy
# of the AgentCore identity from `make certs-pull` (.certs/<cluster>/agentcore-worker/) is
# shredded: in the cloud the identity lives only in the cluster and in Secrets Manager.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
SECRET_ID="" KMS=() CLUSTER=kind CONFIRM="${EXPORT_AGENTCORE_CERT_CONFIRM:-}" CHECK=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --secret-id) SECRET_ID="$2"; shift 2 ;;
    --cluster) CLUSTER="$2"; shift 2 ;;
    --kms-key-id) KMS=(--kms-key-id "$2"); shift 2 ;;
    --yes) CONFIRM=yes; shift ;;
    --check) CHECK=1; shift ;;
    *) echo "usage: $0 --secret-id NAME [--cluster eks|kind] [--kms-key-id KEY] [--yes|--check]" >&2; exit 2 ;;
  esac
done
[[ -n "$SECRET_ID" ]] || { echo "--secret-id is required" >&2; exit 2; }
region="${AWS_REGION:-us-east-1}"
[[ "$region" == us-east-1 ]] || { echo "export-agentcore-cert: us-east-1 only" >&2; exit 2; }
case "$CLUSTER" in
  eks) export KUBECONFIG="$PWD/.temporal/kubeconfig-eks"; LOCAL_COPY=.certs/eks/agentcore-worker
       [[ -f "$KUBECONFIG" ]] || scripts/eks.sh kubeconfig >/dev/null ;;
  kind) export KUBECONFIG="${KUBECONFIG:-$PWD/.temporal/kubeconfig-agentcore-demo}"; LOCAL_COPY=.certs/agentcore-worker ;;
  *) echo "--cluster must be eks or kind" >&2; exit 2 ;;
esac

# Cert dates and CN from a PEM on stdin (no key material printed).
cert_dates() { openssl x509 -noout -subject -issuer -startdate -enddate | sed 's/^/  /'; }

if [[ $CHECK -eq 1 ]]; then
  json="$(aws secretsmanager get-secret-value --region "$region" --secret-id "$SECRET_ID" \
            --query SecretString --output text)"
  echo "export-agentcore-cert: $SECRET_ID present; keys: $(jq -r 'keys | join(",")' <<<"$json")"
  jq -r .tls_crt <<<"$json" | cert_dates
  aws secretsmanager describe-secret --region "$region" --secret-id "$SECRET_ID" \
    --query '{Tags: Tags, KmsKeyId: KmsKeyId, LastChanged: LastChangedDate}' --output json | scripts/redact.sh
  exit 0
fi

if [[ "$CONFIRM" != yes ]]; then
  echo "export-agentcore-cert: this writes Secrets Manager secret '$SECRET_ID' (AWS cost ~\$0.40/mo)." >&2
  echo "Needs the user's go-ahead (G5). Re-run with --yes (make ... CONFIRM=yes)." >&2
  exit 1
fi
kubectl get certificate agentcore-worker-tls -n temporal \
  -o jsonpath='{"certificate agentcore-worker-tls: duration "}{.spec.duration}{", renewBefore "}{.spec.renewBefore}{", Ready="}{.status.conditions[?(@.type=="Ready")].status}{"\n"}'
value="$(kubectl get secret agentcore-worker-tls -n temporal -o json | jq -c '{
  tls_crt: (.data["tls.crt"] | @base64d), tls_key: (.data["tls.key"] | @base64d),
  ca_crt: (.data["ca.crt"] | @base64d)}')"
jq -r .tls_crt <<<"$value" | cert_dates
if aws secretsmanager describe-secret --region "$region" --secret-id "$SECRET_ID" >/dev/null 2>&1; then
  aws secretsmanager put-secret-value --region "$region" --secret-id "$SECRET_ID" \
    --secret-string "$value" >/dev/null
  echo "export-agentcore-cert: new version of $SECRET_ID"
else
  aws secretsmanager create-secret --region "$region" --name "$SECRET_ID" ${KMS[@]+"${KMS[@]}"} \
    --description "AgentCore Support Worker mTLS client cert (CN agentcore-worker) + CA; scripts/export-agentcore-cert.sh" \
    --tags Key=Project,Value=agentcore-self-hosted Key=ManagedBy,Value=scripts/export-agentcore-cert.sh \
    --secret-string "$value" >/dev/null
  echo "export-agentcore-cert: created $SECRET_ID (tag Project=agentcore-self-hosted)"
fi
unset value
if [[ -d "$LOCAL_COPY" ]]; then
  for f in "$LOCAL_COPY"/*; do
    [[ -f "$f" ]] || continue
    if command -v shred >/dev/null; then shred -u "$f"; else rm -P "$f"; fi
  done
  rmdir "$LOCAL_COPY"
  echo "export-agentcore-cert: shredded the local copy $LOCAL_COPY/"
fi
