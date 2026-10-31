#!/usr/bin/env bash
# The EKS cluster from the laptop, without touching ~/.kube/config or its current context.
#   scripts/eks.sh kubeconfig   (make eks-kubeconfig)  write .temporal/kubeconfig-eks, context
#                               agentcore-demo-eks (aws eks update-kubeconfig --kubeconfig ...)
#   scripts/eks.sh env          (make eks-env)         print the KUBECONFIG export for kubectl by hand
#   scripts/eks.sh status       (make eks-status)      nodes, add-ons, LBC and cert-manager (redacted)
# The API endpoint accepts only the laptop's public /32 (20-platform public_access_cidrs): if
# the laptop's IP changed, update infra/terraform/20-platform/terraform.auto.tfvars and re-plan.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
export AWS_REGION=us-east-1 AWS_PAGER=""
CLUSTER="${CLUSTER:-temporal}"
KCFG="$PWD/.temporal/kubeconfig-eks"
CONTEXT=agentcore-demo-eks

case "${1:-}" in
  kubeconfig)
    mkdir -p .temporal
    aws eks update-kubeconfig --name "$CLUSTER" --region "$AWS_REGION" --kubeconfig "$KCFG" --alias "$CONTEXT" \
      | scripts/redact.sh
    chmod 600 "$KCFG"
    ;;
  env)
    echo "export KUBECONFIG=$KCFG"
    ;;
  status)
    [[ -f "$KCFG" ]] || "$0" kubeconfig >/dev/null
    export KUBECONFIG="$KCFG"
    {
      kubectl get nodes -L topology.k8s.aws/zone-id,kubernetes.io/arch,node.kubernetes.io/instance-type
      kubectl -n kube-system get deploy aws-load-balancer-controller coredns
      kubectl -n kube-system get ds aws-node kube-proxy
      kubectl -n cert-manager get deploy
    } 2>&1 | scripts/redact.sh
    ;;
  *) echo "usage: $0 kubeconfig|env|status" >&2; exit 2 ;;
esac
