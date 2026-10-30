#!/usr/bin/env bash
# Support's AgentCore runtime through the AgentCore CLI 1.0.x (ADR-0008: CLI, not Terraform).
#   scripts/agentcore-deploy.sh project          assemble build/agentcore-project from build/agentcore (no AWS)
#   scripts/agentcore-deploy.sh build            project + `agentcore build` (local packaging check)
#   scripts/agentcore-deploy.sh deploy           bundle + project + `agentcore deploy` (make agentcore-deploy MODE=PUBLIC)
#   scripts/agentcore-deploy.sh status           `agentcore status --json` (redacted)
#   scripts/agentcore-deploy.sh runtime-arn | endpoint-arn   (raw ARNs for other scripts; never paste them)
#   scripts/agentcore-deploy.sh down             (make agentcore-down) remove the runtime, delete the CLI stack
#
# Layout. build/agentcore/ is the CodeZip code location (make agentcore-bundle, tested by
# make agentcore-bundle-test). The CLI 1.0.x wants a v1 project (agentcore.json "version": 2,
# agentcore/cdk/), so `project` scaffolds one with `agentcore create --template empty` and
# copies the code to app/support_supervisor/ (codeLocation in agentcore.json.tmpl).
#
# MODE=PUBLIC (v0.8 probe, ADR-0005): no VPC; TEMPORAL_ADDRESS=127.0.0.1:7233 (the Worker
# inside AgentCore cannot reach the laptop and fails within TEMPORAL_CONNECT_TIMEOUT_SECONDS),
# no mTLS secret, idle session timeout 120 s. MODE=VPC: v0.12 (subnets/SGs from Terraform).
# The execution role is the 30-agentcore-iam one (executionRoleArn), not a CLI-created role.
# CLI stack: AgentCore-<project>-default (project AgentcoreDemo).
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
export AWS_REGION="${AWS_REGION:-us-east-1}"
PROJECT="${AGENTCORE_PROJECT_NAME:-AgentcoreDemo}"
RUNTIME="${AGENTCORE_RUNTIME_NAME:-support_supervisor}"
MODE="${MODE:-PUBLIC}"
P=build/agentcore-project
AC=scripts/agentcore.sh
STACK="AgentCore-$PROJECT-default"
export AGENTCORE_PROJECT_NAME="$PROJECT" AGENTCORE_RUNTIME_NAME="$RUNTIME"

build_id() { echo "${TEMPORAL_BUILD_ID:-$(scripts/build-id.sh)}"; }

mode_env() {
  export AGENTCORE_NETWORK_MODE="$MODE"
  if [[ "$MODE" == PUBLIC ]]; then
    export AGENTCORE_TEMPORAL_ADDRESS="${AGENTCORE_TEMPORAL_ADDRESS:-127.0.0.1:7233}"
    export AGENTCORE_TEMPORAL_TLS="${AGENTCORE_TEMPORAL_TLS:-false}"
    export TEMPORAL_MTLS_SECRET_ID="${TEMPORAL_MTLS_SECRET_ID-}"
    export AGENTCORE_TEMPORAL_TLS_SERVER_NAME="${AGENTCORE_TEMPORAL_TLS_SERVER_NAME-}"
    export AGENTCORE_IDLE_SESSION_TIMEOUT="${AGENTCORE_IDLE_SESSION_TIMEOUT:-120}"
  fi
}

project() {
  [[ -f build/agentcore/agentcore_worker.py ]] || { echo "agentcore-deploy: run make agentcore-bundle first" >&2; exit 1; }
  if [[ ! -f "$P/agentcore/cdk/cdk.json" ]]; then
    rm -rf "$P" "build/$PROJECT"
    (cd build && "../$AC" create --name "$PROJECT" --template empty --skip-install --skip-git >/dev/null 2>&1)
    mv "build/$PROJECT" "$P"
  fi
  # The CDK app the CLI scaffolds (@aws/agentcore-cdk pinned exactly by the CLI's template).
  if [[ ! -d "$P/agentcore/cdk/node_modules" ]]; then
    (cd "$P/agentcore/cdk" && npm install --no-fund --no-audit >/dev/null 2>&1)
  fi
  rm -rf "$P/app/$RUNTIME" "$P/agentcore/cdk/cdk.out"; mkdir -p "$P/app/$RUNTIME"
  (cd build/agentcore && tar --exclude ./agentcore -cf - .) | tar -xf - -C "$P/app/$RUNTIME"
  cp build/agentcore/agentcore/agentcore.json "$P/agentcore/agentcore.json"
  if [[ -f build/agentcore/agentcore/aws-targets.json ]]; then
    cp build/agentcore/agentcore/aws-targets.json "$P/agentcore/aws-targets.json"
  fi
  echo "agentcore-deploy: project $P ($PROJECT, runtime $RUNTIME, Build ID $(cat "$P/app/$RUNTIME/BUILD_ID"), network $MODE)"
}

runtime_id() {
  aws bedrock-agentcore-control list-agent-runtimes --region "$AWS_REGION" \
    --query "agentRuntimes[?agentRuntimeName=='${PROJECT}_${RUNTIME}'].agentRuntimeId | [0]" --output text
}

case "${1:-}" in
  project) mode_env; project ;;
  build)
    mode_env; project
    (cd "$P" && "../../$AC" build)
    ;;
  deploy)
    mode_env
    [[ "$MODE" == PUBLIC || "$MODE" == VPC ]] || { echo "MODE must be PUBLIC or VPC" >&2; exit 2; }
    tf_out() { .tools/bin/terraform -chdir=infra/terraform/30-agentcore-iam output -raw "$1"; }
    AGENTCORE_EXECUTION_ROLE_ARN="${AGENTCORE_EXECUTION_ROLE_ARN:-$(tf_out execution_role_arn)}"
    [[ -n "$AGENTCORE_EXECUTION_ROLE_ARN" ]] || { echo "no execution role: make tf-apply STACK=30-agentcore-iam" >&2; exit 1; }
    AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
    export AGENTCORE_EXECUTION_ROLE_ARN AWS_ACCOUNT_ID
    scripts/agentcore-bundle.sh
    project
    (cd "$P" && "../../$AC" deploy --target default) 2>&1 | scripts/redact.sh
    "$0" status
    ;;
  status)
    (cd "$P" && "../../$AC" status --target default --json) 2>&1 | scripts/redact.sh
    ;;
  runtime-arn)
    id="$(runtime_id)"; [[ "$id" != None && -n "$id" ]] || { echo "no runtime ${PROJECT}_${RUNTIME}" >&2; exit 1; }
    aws bedrock-agentcore-control get-agent-runtime --region "$AWS_REGION" --agent-runtime-id "$id" \
      --query agentRuntimeArn --output text
    ;;
  endpoint-arn)
    id="$(runtime_id)"; [[ "$id" != None && -n "$id" ]] || { echo "no runtime ${PROJECT}_${RUNTIME}" >&2; exit 1; }
    name="b_$(build_id | cut -c1-7)"
    aws bedrock-agentcore-control list-agent-runtime-endpoints --region "$AWS_REGION" --agent-runtime-id "$id" \
      --query "runtimeEndpoints[?name=='$name'].agentRuntimeEndpointArn | [0]" --output text
    ;;
  down)
    # The CLI 1.0.1 has no `destroy` (ledger 45): empty the project, then `deploy --yes`
    # removes the target's stack. Fallback: delete the stack directly, only if it is ours.
    if [[ -f "$P/agentcore/agentcore.json" ]]; then
      (cd "$P" && "../../$AC" remove all --yes && "../../$AC" deploy --target default --yes) 2>&1 | scripts/redact.sh || true
    fi
    if aws cloudformation describe-stacks --region "$AWS_REGION" --stack-name "$STACK" >/dev/null 2>&1; then
      echo "agentcore-deploy: stack $STACK still present; deleting it"
      aws cloudformation delete-stack --region "$AWS_REGION" --stack-name "$STACK"
      aws cloudformation wait stack-delete-complete --region "$AWS_REGION" --stack-name "$STACK"
    fi
    id="$(runtime_id)"
    if [[ "$id" == None || -z "$id" ]]; then
      echo "agentcore-deploy: runtime ${PROJECT}_${RUNTIME} gone, stack $STACK gone"
    else
      echo "agentcore-deploy: runtime ${PROJECT}_${RUNTIME} still exists" >&2; exit 1
    fi
    ;;
  *) echo "usage: $0 project|build|deploy|status|runtime-arn|endpoint-arn|down" >&2; exit 2 ;;
esac
