#!/usr/bin/env bash
# Support's AgentCore runtime through the AgentCore CLI 1.0.x (ADR-0008: CLI, not Terraform).
#   scripts/agentcore-deploy.sh project          assemble build/agentcore-project from build/agentcore (no AWS)
#   scripts/agentcore-deploy.sh build            project + `agentcore build` (local packaging check)
#   scripts/agentcore-deploy.sh deploy           bundle + project + `agentcore deploy` (make agentcore-deploy MODE=PUBLIC)
#   scripts/agentcore-deploy.sh status           `agentcore status --json` (redacted)
#   scripts/agentcore-deploy.sh runtime-arn | endpoint-arn   (raw ARNs for other scripts; never paste them)
#   scripts/agentcore-deploy.sh down             (make agentcore-down) remove the runtime, delete the CLI stack
#   scripts/agentcore-deploy.sh network          (make agentcore-network) get-agent-runtime: network, role, lifecycle, endpoints
#   scripts/agentcore-deploy.sh logs [MIN]       (make agentcore-logs) the b_<build7> endpoint's log lines of the last MIN minutes
#
# Layout. build/agentcore/ is the CodeZip code location (make agentcore-bundle, tested by
# make agentcore-bundle-test). The CLI 1.0.x wants a v1 project (agentcore.json "version": 2,
# agentcore/cdk/), so `project` scaffolds one with `agentcore create --template empty` and
# copies the code to app/support_supervisor/ (codeLocation in agentcore.json.tmpl).
#
# MODE=PUBLIC (v0.8 probe, ADR-0005): no VPC; TEMPORAL_ADDRESS=127.0.0.1:7233 (the Worker
# inside AgentCore cannot reach the laptop and fails within TEMPORAL_CONNECT_TIMEOUT_SECONDS),
# no mTLS secret, idle session timeout 120 s. MODE=VPC (v0.12, the demo runtime): the private
# subnets, sg-agentcore-worker, the execution role and the Route 53 frontend name come from
# .generated/foundation.env (make render-config); TLS on, server name = the Route 53 name, mTLS
# from Secrets Manager temporal/agentcore-worker-mtls, debounce 30 s, idle timeout 120 s
# (ADR-0006 v0.12: an idle session costs while it waits; the Worker has already drained).
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
TARGET="${AGENTCORE_TARGET:-default}"
STACK="AgentCore-$PROJECT-$TARGET"
# The CLI 1.0.1 names the runtime <project>_<target>_<runtime> (seen at the v0.8 deploy).
RUNTIME_FULL="${PROJECT}_${TARGET}_${RUNTIME}"
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
  else
    [[ -f .generated/foundation.env ]] || { echo "agentcore-deploy: make render-config first (.generated/foundation.env)" >&2; exit 1; }
    # Runtime-injected values win (plan 3.2): only fill what the caller did not set.
    local k v
    while IFS='=' read -r k v; do
      k="${k#export }"
      case "$k" in
        AGENTCORE_SUBNETS|AGENTCORE_SECURITY_GROUPS|AGENTCORE_TEMPORAL_ADDRESS|AGENTCORE_TEMPORAL_TLS_SERVER_NAME|AGENTCORE_EXECUTION_ROLE_ARN)
          [[ -n "${!k:-}" ]] || export "$k=$(eval "printf '%s' $v")" ;;
      esac
    done < <(grep '^export ' .generated/foundation.env)
    export AGENTCORE_TEMPORAL_TLS="${AGENTCORE_TEMPORAL_TLS:-true}"
    export TEMPORAL_MTLS_SECRET_ID="${TEMPORAL_MTLS_SECRET_ID:-temporal/agentcore-worker-mtls}"
    export AGENTCORE_CLOUD_DEBOUNCE_SECONDS="${AGENTCORE_CLOUD_DEBOUNCE_SECONDS:-30}"
    export AGENTCORE_IDLE_SESSION_TIMEOUT="${AGENTCORE_IDLE_SESSION_TIMEOUT:-120}"
    [[ "$AGENTCORE_SUBNETS" == subnet-* && "$AGENTCORE_SECURITY_GROUPS" == sg-* ]] ||
      { echo "agentcore-deploy: VPC mode needs AGENTCORE_SUBNETS / AGENTCORE_SECURITY_GROUPS" >&2; exit 1; }
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
    --query "agentRuntimes[?agentRuntimeName=='${RUNTIME_FULL}'].agentRuntimeId | [0]" --output text
}

case "${1:-}" in
  project) mode_env; project ;;
  build)
    mode_env; scripts/agentcore-bundle.sh >/dev/null; project
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
    id="$(runtime_id)"; [[ "$id" != None && -n "$id" ]] || { echo "no runtime ${RUNTIME_FULL}" >&2; exit 1; }
    aws bedrock-agentcore-control get-agent-runtime --region "$AWS_REGION" --agent-runtime-id "$id" \
      --query agentRuntimeArn --output text
    ;;
  endpoint-arn)
    id="$(runtime_id)"; [[ "$id" != None && -n "$id" ]] || { echo "no runtime ${RUNTIME_FULL}" >&2; exit 1; }
    name="b_$(build_id | cut -c1-7)"
    aws bedrock-agentcore-control list-agent-runtime-endpoints --region "$AWS_REGION" --agent-runtime-id "$id" \
      --query "runtimeEndpoints[?name=='$name'].agentRuntimeEndpointArn | [0]" --output text
    ;;
  network)
    id="$(runtime_id)"; [[ "$id" != None && -n "$id" ]] || { echo "no runtime ${RUNTIME_FULL}" >&2; exit 1; }
    aws bedrock-agentcore-control get-agent-runtime --region "$AWS_REGION" --agent-runtime-id "$id" \
      --query '{name: agentRuntimeName, status: status, version: agentRuntimeVersion, networkConfiguration: networkConfiguration, role: roleArn, lifecycle: lifecycleConfiguration, protocol: protocolConfiguration, authorizer: authorizerConfiguration}' \
      --output json | scripts/redact.sh
    aws bedrock-agentcore-control list-agent-runtime-endpoints --region "$AWS_REGION" --agent-runtime-id "$id" \
      --query 'runtimeEndpoints[].{name: name, status: status, liveVersion: liveVersion}' --output json | scripts/redact.sh
    ;;
  logs)
    id="$(runtime_id)"; [[ "$id" != None && -n "$id" ]] || { echo "no runtime ${RUNTIME_FULL}" >&2; exit 1; }
    mins="${2:-30}"; name="b_$(build_id | cut -c1-7)"
    lg="/aws/bedrock-agentcore/runtimes/${id}-${name}"
    start=$(( ($(date +%s) - mins * 60) * 1000 ))
    echo "log group ${lg} (last ${mins} min)" | scripts/redact.sh
    aws logs filter-log-events --region "$AWS_REGION" --log-group-name "$lg" --start-time "$start" \
      --query 'events[].message' --output json 2>/dev/null \
      | jq -r '.[] | rtrimstr("\n")' | grep -v '^[[:space:]]*$' | scripts/redact.sh || true
    ;;
  down)
    # The CLI 1.0.1 has no `destroy` (ledger 45): empty the project, then `deploy --yes`
    # removes the target's stack. Fallback: delete the stack directly, only if it is ours.
    if [[ -f "$P/agentcore/agentcore.json" ]]; then
      (cd "$P" && "../../$AC" remove all --yes && "../../$AC" deploy --target default --yes) 2>&1 | scripts/redact.sh || true
    fi
    if aws cloudformation describe-stacks --region "$AWS_REGION" --stack-name "$STACK" >/dev/null 2>&1; then
      # The CLI tags its stack only agentcore:project-name / agentcore:target-name (the
      # Project tag reaches the runtime, not the stack): delete only a stack of this project.
      owner="$(aws cloudformation describe-stacks --region "$AWS_REGION" --stack-name "$STACK" \
        --query "Stacks[0].Tags[?Key=='agentcore:project-name'].Value | [0]" --output text)"
      [[ "$owner" == "$PROJECT" ]] || { echo "agentcore-deploy: stack $STACK is not tagged agentcore:project-name=$PROJECT; refusing" >&2; exit 1; }
      echo "agentcore-deploy: stack $STACK still present; deleting it"
      aws cloudformation delete-stack --region "$AWS_REGION" --stack-name "$STACK"
      aws cloudformation wait stack-delete-complete --region "$AWS_REGION" --stack-name "$STACK"
    fi
    id="$(runtime_id)"
    if [[ "$id" == None || -z "$id" ]]; then
      # AgentCore leaves the runtime's log groups (never-expire retention) behind:
      # /aws/bedrock-agentcore/runtimes/<runtime name>-<10 chars>-<endpoint>. Only our name.
      for lg in $(aws logs describe-log-groups --region "$AWS_REGION" \
                    --log-group-name-prefix "/aws/bedrock-agentcore/runtimes/${RUNTIME_FULL}-" \
                    --query 'logGroups[].logGroupName' --output text); do
        aws logs delete-log-group --region "$AWS_REGION" --log-group-name "$lg"
        echo "agentcore-deploy: deleted log group $lg" | scripts/redact.sh
      done
      echo "agentcore-deploy: runtime ${RUNTIME_FULL} gone, stack $STACK gone"
    else
      echo "agentcore-deploy: runtime ${RUNTIME_FULL} still exists" >&2; exit 1
    fi
    ;;
  *) echo "usage: $0 project|build|deploy|status|runtime-arn|endpoint-arn|network|logs|down" >&2; exit 2 ;;
esac
