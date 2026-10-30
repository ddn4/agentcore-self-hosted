#!/usr/bin/env bash
# External ID for the AgentCore invocation role (runbook 6.1, ADR-0007). Never in Terraform
# state or git: it lives only in Secrets Manager and is read at run time.
#   scripts/external-id.sh           (make external-id) create the secret if absent (idempotent)
#   scripts/external-id.sh --check   exit 0 if it exists; print its name and length, never the value
#   scripts/external-id.sh --value   print the value (for scripts that pipe it straight on)
set -euo pipefail
NAME="${EXTERNAL_ID_SECRET:-temporal/agentcore-external-id}"
REGION="${AWS_REGION:-us-east-1}"
aws_() { aws --region "$REGION" "$@"; }

exists() { aws_ secretsmanager describe-secret --secret-id "$NAME" >/dev/null 2>&1; }

case "${1:-create}" in
  create)
    if exists; then
      echo "external-id: secret $NAME exists (kept)"
    else
      # 32 hex chars: inside the template's [a-zA-Z0-9_+=,.@-], 5-45.
      value="$(openssl rand -hex 16)"
      aws_ secretsmanager create-secret --name "$NAME" \
        --description "External ID for the Temporal AgentCore invocation role (agentcore-self-hosted)" \
        --secret-string "$value" \
        --tags Key=Project,Value=agentcore-self-hosted Key=ManagedBy,Value=scripts/external-id.sh >/dev/null
      unset value
      echo "external-id: created secret $NAME"
    fi
    ;;
  --check)
    exists || { echo "external-id: secret $NAME missing (make external-id)" >&2; exit 1; }
    len="$(aws_ secretsmanager get-secret-value --secret-id "$NAME" --query SecretString --output text | tr -d '\n' | wc -c | tr -d ' ')"
    echo "external-id: secret $NAME present, $len characters"
    ;;
  --value)
    aws_ secretsmanager get-secret-value --secret-id "$NAME" --query SecretString --output text
    ;;
  *) echo "usage: $0 [create|--check|--value]" >&2; exit 2 ;;
esac
