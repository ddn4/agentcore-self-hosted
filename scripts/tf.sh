#!/usr/bin/env bash
# Terraform for infra/terraform/<stack> with the project-local binaries (make tools).
#   scripts/tf.sh static                 fmt -check, init -backend=false, validate, tflint (no AWS)
#   scripts/tf.sh plan STACK [args]      writes STACK/tfplan (gitignored; review it)   (make tf-plan STACK=...)
#   scripts/tf.sh apply STACK            applies exactly that saved plan, then deletes it (make tf-apply STACK=...)
#   scripts/tf.sh destroy STACK [args]   (make tf-destroy STACK=...) after guards (destroy_guard below)
#   scripts/tf.sh destroy foundation     20-platform then 10-network (v0.10 reverse order; 00/30 are
#                                        kept until v1.0 down-all and need STACK= explicitly)
#   DRY_RUN=1 scripts/tf.sh destroy ...  read-only: the guards plus `plan -destroy` (no changes made)
#   scripts/tf.sh output STACK [args...]
# State is local per stack (gitignored); .terraform.lock.hcl is committed. Every stack
# tags its resources Project=agentcore-self-hosted (provider default_tags).
# Region is us-east-1 only; plan/apply refuse any other AWS_REGION.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
TF="$PWD/.tools/bin/terraform"
TFLINT="$PWD/.tools/bin/tflint"
ROOT=infra/terraform
[[ -x "$TF" ]] || { echo "tf: run 'make tools' first (.tools/bin/terraform)" >&2; exit 1; }
export TF_PLUGIN_CACHE_DIR="$PWD/.tools/tf-plugin-cache" TF_IN_AUTOMATION=1 TF_INPUT=0
mkdir -p "$TF_PLUGIN_CACHE_DIR"

stacks() { find "$ROOT" -mindepth 1 -maxdepth 1 -type d -name '[0-9]*' | sort; }

# Refuse a destroy that would orphan AWS resources Terraform does not know about.
#  20-platform: Helm releases other than the two Terraform-managed ones (v0.11 temporal) and
#    Services of type LoadBalancer (their NLB is the LBC's, not Terraform's) must be gone first:
#    `helm uninstall` before the destroy (plan v0.10 "after a Helm uninstall").
#  10-network: 20-platform must be empty, and no ENIs other than the endpoints' and the NAT's may
#    remain (AgentCore VPC-mode ENIs linger up to 8 h after a runtime is deleted; plan v0.12).
destroy_guard() {
  local stack="$1" kcfg=.temporal/kubeconfig-eks
  case "$stack" in
    20-platform)
      local st
      st="$("$TF" -chdir="$ROOT/20-platform" state list 2>/dev/null || true)"
      if grep -q '^module.eks.aws_eks_cluster' <<<"$st"; then
        scripts/eks.sh kubeconfig >/dev/null
        local extra lbs
        extra="$(KUBECONFIG=$kcfg helm list -A -q | grep -vxE 'aws-load-balancer-controller|cert-manager' || true)"
        lbs="$(KUBECONFIG=$kcfg kubectl get svc -A -o jsonpath='{range .items[?(@.spec.type=="LoadBalancer")]}{.metadata.namespace}/{.metadata.name}{"\n"}{end}')"
        [[ -z "$extra" ]] || { echo "tf: helm uninstall these first: $extra" >&2; return 1; }
        [[ -z "$lbs" ]] || { echo "tf: LoadBalancer Services still exist (their NLBs would be orphaned): $lbs" >&2; return 1; }
        echo "tf: guard ok: only the Terraform-managed Helm releases, no LoadBalancer Services"
      fi ;;
    10-network)
      if [[ -s "$ROOT/20-platform/terraform.tfstate" ]] && [[ -n "$("$TF" -chdir="$ROOT/20-platform" state list 2>/dev/null)" ]]; then
        echo "tf: destroy 20-platform first" >&2; return 1
      fi
      local vpc others
      vpc="$("$TF" -chdir="$ROOT/10-network" output -raw vpc_id 2>/dev/null || true)"
      if [[ -n "$vpc" ]]; then
        others="$(aws ec2 describe-network-interfaces --region us-east-1 --filters Name=vpc-id,Values="$vpc" \
          --query 'NetworkInterfaces[?InterfaceType!=`vpc_endpoint` && InterfaceType!=`nat_gateway`].[InterfaceType,Description]' --output text)"
        [[ -z "$others" ]] || { echo "tf: ENIs still in the VPC (AgentCore ENIs can linger up to 8 h):" >&2; echo "$others" | scripts/redact.sh >&2; return 1; }
        echo "tf: guard ok: no ENIs besides the endpoints' and the NAT's"
      fi ;;
  esac
}

cmd="${1:-}"; shift || true
case "$cmd" in
  static)
    "$TF" fmt -check -recursive -diff "$ROOT"
    echo "tf: fmt ok"
    for dir in $(stacks); do
      # A throwaway data dir: never touches the stack's real .terraform/ or state.
      data="$(mktemp -d)"
      TF_DATA_DIR="$data" "$TF" -chdir="$dir" init -backend=false -lockfile=readonly >/dev/null
      TF_DATA_DIR="$data" "$TF" -chdir="$dir" validate -no-color
      rm -rf "$data"
    done
    if [[ -x "$TFLINT" ]]; then
      (cd "$ROOT" && "$TFLINT" --recursive --config "$PWD/.tflint.hcl" --format compact)
      echo "tf: tflint ok"
    else
      echo "tf: tflint missing (make tools)" >&2; exit 1
    fi
    ;;
  destroy)
    target="${1:?usage: $0 destroy STACK|foundation}"; shift
    if [[ "$target" == foundation ]]; then order=(20-platform 10-network); else order=("$target"); fi
    for stack in "${order[@]}"; do
      [[ -d "$ROOT/$stack" ]] || { echo "tf: no stack $ROOT/$stack" >&2; exit 2; }
      "$TF" -chdir="$ROOT/$stack" init -lockfile=readonly >/dev/null
      if [[ "${DRY_RUN:-0}" == 1 ]]; then
        echo "tf: DRY_RUN: guard and plan -destroy for $stack (nothing is changed)"
        destroy_guard "$stack" || echo "tf: DRY_RUN: the guard would refuse $stack now" >&2
        "$TF" -chdir="$ROOT/$stack" plan -destroy -no-color -lock=false "$@" | grep -E '^Plan:|No changes'
      else
        destroy_guard "$stack"
        "$TF" -chdir="$ROOT/$stack" destroy -auto-approve -no-color "$@"
      fi
    done
    ;;
  plan|apply|output|init)
    stack="${1:?usage: $0 $cmd STACK}"; shift
    dir="$ROOT/$stack"
    [[ -d "$dir" ]] || { echo "tf: no stack $dir" >&2; exit 2; }
    if [[ "$cmd" != output && "${AWS_REGION:-us-east-1}" != us-east-1 ]]; then
      echo "tf: this project uses us-east-1 only (AWS_REGION=$AWS_REGION)" >&2; exit 2
    fi
    "$TF" -chdir="$dir" init -lockfile=readonly >/dev/null
    case "$cmd" in
      init) ;;
      output) "$TF" -chdir="$dir" output "$@" ;;
      plan) "$TF" -chdir="$dir" plan -no-color -out=tfplan "$@" ;;
      apply)
        # Only a reviewed plan is applied (make tf-plan first); the plan file can hold
        # sensitive inputs, so it is removed afterwards.
        [[ -f "$dir/tfplan" ]] || { echo "tf: no saved plan; run make tf-plan STACK=$stack and review it" >&2; exit 1; }
        "$TF" -chdir="$dir" apply -no-color "$@" tfplan
        rm -f "$dir/tfplan"
        ;;
    esac
    ;;
  lock)
    # Refresh .terraform.lock.hcl for the platforms the project runs on (laptop + CI).
    for dir in $(stacks); do
      "$TF" -chdir="$dir" providers lock -platform=darwin_arm64 -platform=linux_amd64 >/dev/null
      echo "tf: locked $dir"
    done
    ;;
  *)
    echo "usage: $0 static | plan|apply|destroy|output|init STACK [args] | lock" >&2; exit 2 ;;
esac
