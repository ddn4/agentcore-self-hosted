#!/usr/bin/env bash
# The AgentCore invocation role (runbook Phase 6) as a CloudFormation stack, managed ONLY
# here, never by Terraform: the External ID is a stack parameter and must stay out of
# Terraform state (ADR-0007). It is read from Secrets Manager at run time.
#   scripts/invoke-role.sh create --temporal-role-arn ARN [--runtime-arn ARN]   (make invoke-role)
#   scripts/invoke-role.sh update [--temporal-role-arn ARN] [--runtime-arn ARN]  (e.g. after an External ID rotation)
#   scripts/invoke-role.sh delete
#   scripts/invoke-role.sh status | arn
# --temporal-role-arn: the identity the WCI runs as: temporal-dev-wci (v0.8, laptop dev
#   server) or temporal-server-wci (IRSA, v0.12). An IAM role ARN, never an assumed-role ARN.
# --runtime-arn: the AgentCore runtime; the stack gets "<runtime ARN>*" so endpoints are
#   covered (template doc). Default: scripts/agentcore-deploy.sh runtime-arn.
# Template: deploy/agentcore/temporal-self-hosted-serverless-worker-agentcore-role.yaml
# (make cfn-template; sha256-pinned). Stack tags Project=agentcore-self-hosted reach the role.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
export AWS_REGION="${AWS_REGION:-us-east-1}"
STACK="${INVOKE_ROLE_STACK:-temporal-agentcore-worker}"
ROLE_NAME="${INVOKE_ROLE_NAME:-Temporal-AgentCore-Worker}"
TEMPLATE=deploy/agentcore/temporal-self-hosted-serverless-worker-agentcore-role.yaml
cfn() { aws --region "$AWS_REGION" cloudformation "$@"; }

cmd="${1:-status}"; shift || true
TEMPORAL_ROLE_ARN="" RUNTIME_ARN=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --temporal-role-arn) TEMPORAL_ROLE_ARN="$2"; shift 2 ;;
    --runtime-arn) RUNTIME_ARN="$2"; shift 2 ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
done

stack_status() { cfn describe-stacks --stack-name "$STACK" --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo ABSENT; }

params() {
  [[ "$TEMPORAL_ROLE_ARN" == arn:aws:iam::*:role/* ]] ||
    { echo "invoke-role: --temporal-role-arn must be an IAM role ARN (got '${TEMPORAL_ROLE_ARN:-}')" >&2; exit 2; }
  [[ -n "$RUNTIME_ARN" ]] || RUNTIME_ARN="$(scripts/agentcore-deploy.sh runtime-arn)"
  [[ "$RUNTIME_ARN" == arn:aws:bedrock-agentcore:*:runtime/* ]] ||
    { echo "invoke-role: no runtime ARN (deploy the runtime first or pass --runtime-arn)" >&2; exit 2; }
  scripts/cfn-template.sh --check >/dev/null
  local ext; ext="$(scripts/external-id.sh --value)"
  # The External ID goes straight into the API call (process args, never a file or log).
  PARAMS=(ParameterKey=TemporalIamRoleArn,ParameterValue="$TEMPORAL_ROLE_ARN"
          ParameterKey=AssumeRoleExternalId,ParameterValue="$ext"
          "ParameterKey=AgentRuntimeARNs,ParameterValue=${RUNTIME_ARN}*"
          ParameterKey=RoleName,ParameterValue="$ROLE_NAME")
}

case "$cmd" in
  create)
    [[ "$(stack_status)" == ABSENT ]] || { echo "invoke-role: stack $STACK exists ($(stack_status)); use update" >&2; exit 1; }
    params
    cfn create-stack --stack-name "$STACK" --template-body "file://$TEMPLATE" \
      --parameters "${PARAMS[@]}" --capabilities CAPABILITY_NAMED_IAM \
      --tags Key=Project,Value=agentcore-self-hosted Key=ManagedBy,Value=scripts/invoke-role.sh >/dev/null
    cfn wait stack-create-complete --stack-name "$STACK"
    echo "invoke-role: stack $STACK $(stack_status); role $ROLE_NAME"
    ;;
  update)
    [[ "$(stack_status)" != ABSENT ]] || { echo "invoke-role: no stack $STACK" >&2; exit 1; }
    if [[ -z "$TEMPORAL_ROLE_ARN" ]]; then
      TEMPORAL_ROLE_ARN="$(cfn describe-stacks --stack-name "$STACK" \
        --query "Stacks[0].Parameters[?ParameterKey=='TemporalIamRoleArn'].ParameterValue | [0]" --output text)"
    fi
    params
    if out="$(cfn update-stack --stack-name "$STACK" --template-body "file://$TEMPLATE" \
                --parameters "${PARAMS[@]}" --capabilities CAPABILITY_NAMED_IAM 2>&1)"; then
      cfn wait stack-update-complete --stack-name "$STACK"
    elif [[ "$out" == *"No updates are to be performed"* ]]; then
      echo "invoke-role: no changes"
    else
      echo "$out" | scripts/redact.sh >&2; exit 1
    fi
    echo "invoke-role: stack $STACK $(stack_status)"
    ;;
  delete)
    st="$(stack_status)"
    if [[ "$st" == ABSENT ]]; then echo "invoke-role: stack $STACK absent"; exit 0; fi
    tag="$(cfn describe-stacks --stack-name "$STACK" --query "Stacks[0].Tags[?Key=='Project'].Value | [0]" --output text)"
    [[ "$tag" == agentcore-self-hosted ]] || { echo "invoke-role: stack $STACK is not tagged Project=agentcore-self-hosted; refusing" >&2; exit 1; }
    cfn delete-stack --stack-name "$STACK"
    cfn wait stack-delete-complete --stack-name "$STACK"
    echo "invoke-role: stack $STACK deleted (role $ROLE_NAME gone)"
    ;;
  status)
    echo "invoke-role: stack $STACK $(stack_status)"
    if [[ "$(stack_status)" != ABSENT ]]; then
      cfn describe-stacks --stack-name "$STACK" --query 'Stacks[0].{Parameters: Parameters[?ParameterKey!=`AssumeRoleExternalId`], Outputs: Outputs}' \
        --output json | scripts/redact.sh
    fi
    ;;
  arn)
    cfn describe-stacks --stack-name "$STACK" --query "Stacks[0].Outputs[?OutputKey=='RoleARN'].OutputValue | [0]" --output text
    ;;
  *) echo "usage: $0 create|update|delete|status|arn [--temporal-role-arn ARN] [--runtime-arn ARN]" >&2; exit 2 ;;
esac
