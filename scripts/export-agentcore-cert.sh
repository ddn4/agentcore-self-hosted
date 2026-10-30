#!/usr/bin/env bash
# Export the AgentCore Worker client cert (CN agentcore-worker) from the cluster to
# AWS Secrets Manager in the format support_agent.mtls reads: {"tls_crt","tls_key","ca_crt"}
# (runbook 2.6 step 5, 5.3).
#   scripts/export-agentcore-cert.sh --secret-id NAME [--kms-key-id KEY]   (make export-agentcore-cert)
# kind (optional SM path, go-ahead G5): --secret-id temporal-kind/agentcore-worker-mtls
#   (outside the execution role's temporal/agentcore-worker-mtls-* pattern on purpose).
# EKS (v0.12): --secret-id temporal/agentcore-worker-mtls --kms-key-id <cmk>.
# CREATES OR UPDATES AN AWS RESOURCE: refuses unless EXPORT_AGENTCORE_CERT_CONFIRM=yes.
# create-secret the first time, put-secret-value on renewal. The PEMs never touch disk:
# they go from kubectl straight into the --secret-string argument.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
export KUBECONFIG="${KUBECONFIG:-$PWD/.temporal/kubeconfig-agentcore-demo}"
SECRET_ID="" KMS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --secret-id) SECRET_ID="$2"; shift 2 ;;
    --kms-key-id) KMS=(--kms-key-id "$2"); shift 2 ;;
    *) echo "usage: $0 --secret-id NAME [--kms-key-id KEY]" >&2; exit 2 ;;
  esac
done
[[ -n "$SECRET_ID" ]] || { echo "--secret-id is required" >&2; exit 2; }
if [[ "${EXPORT_AGENTCORE_CERT_CONFIRM:-}" != "yes" ]]; then
  echo "export-agentcore-cert: this writes Secrets Manager secret '$SECRET_ID' (AWS cost ~\$0.40/mo)." >&2
  echo "Needs the user's go-ahead (G5). Re-run with EXPORT_AGENTCORE_CERT_CONFIRM=yes." >&2
  exit 1
fi
value="$(kubectl get secret agentcore-worker-tls -n temporal -o json | jq -c '{
  tls_crt: (.data["tls.crt"] | @base64d), tls_key: (.data["tls.key"] | @base64d),
  ca_crt: (.data["ca.crt"] | @base64d)}')"
region="${AWS_REGION:-us-east-1}"
if aws secretsmanager describe-secret --region "$region" --secret-id "$SECRET_ID" >/dev/null 2>&1; then
  aws secretsmanager put-secret-value --region "$region" --secret-id "$SECRET_ID" \
    --secret-string "$value" >/dev/null
  echo "export-agentcore-cert: new version of $SECRET_ID"
else
  aws secretsmanager create-secret --region "$region" --name "$SECRET_ID" ${KMS[@]+"${KMS[@]}"} \
    --tags Key=project,Value=temporal-customer-support Key=managed-by,Value=script \
    --secret-string "$value" >/dev/null
  echo "export-agentcore-cert: created $SECRET_ID"
fi
