#!/usr/bin/env bash
# Terraform for infra/terraform/<stack> with the project-local binaries (make tools).
#   scripts/tf.sh static                 fmt -check, init -backend=false, validate, tflint (no AWS)
#   scripts/tf.sh plan STACK [args]      writes STACK/tfplan (gitignored; review it)   (make tf-plan STACK=...)
#   scripts/tf.sh apply STACK            applies exactly that saved plan, then deletes it (make tf-apply STACK=...)
#   scripts/tf.sh destroy STACK [args]   (make tf-destroy STACK=...)
#   scripts/tf.sh output STACK [args...]
# State is local per stack (gitignored); .terraform.lock.hcl is committed. Every stack
# tags its resources Project=agentcore-self-hosted (provider default_tags).
# Region is us-east-1 only; plan/apply/destroy refuse any other AWS_REGION.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
TF="$PWD/.tools/bin/terraform"
TFLINT="$PWD/.tools/bin/tflint"
ROOT=infra/terraform
[[ -x "$TF" ]] || { echo "tf: run 'make tools' first (.tools/bin/terraform)" >&2; exit 1; }
export TF_PLUGIN_CACHE_DIR="$PWD/.tools/tf-plugin-cache" TF_IN_AUTOMATION=1 TF_INPUT=0
mkdir -p "$TF_PLUGIN_CACHE_DIR"

stacks() { find "$ROOT" -mindepth 1 -maxdepth 1 -type d -name '[0-9]*' | sort; }

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
  plan|apply|destroy|output|init)
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
      destroy) "$TF" -chdir="$dir" destroy -auto-approve -no-color "$@" ;;
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
