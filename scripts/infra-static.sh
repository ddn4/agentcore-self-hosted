#!/usr/bin/env bash
# Static checks for everything under deploy/ (plan 3.1 `infra-static`; part of make check).
#   scripts/infra-static.sh        (make infra-static)
# 1. helm template chart temporal-1.7.0 with each values set we install (plaintext and
#    mTLS, kind) -> kubeconform -strict.
# 2. kubectl kustomize every base and overlay -> kubeconform -strict (cert-manager CRDs
#    from the datree CRDs-catalog schemas).
# 3. deploy/kind/*.yaml -> kubeconform -strict (the kind Cluster config is skipped: not a
#    Kubernetes API object).
# kubeconform comes from PATH or .tools/bin (make tools). Without it, step 1-3 still
# render (a template or kustomize error fails) and the output is only parsed as YAML.
# The chart is cached in .tools/charts after the first download (needs network once).
# No cluster and no AWS needed.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
CHART_VERSION=1.7.0
PATH="$PWD/.tools/bin:$PATH"
tgz=".tools/charts/temporal-$CHART_VERSION.tgz"
if [[ ! -f "$tgz" ]]; then
  mkdir -p .tools/charts
  helm pull temporal --repo https://go.temporal.io/helm-charts --version "$CHART_VERSION" -d .tools/charts >/dev/null
fi
if command -v kubeconform >/dev/null; then
  validate() { kubeconform -strict -summary -output text \
    -schema-location default \
    -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' -; }
else
  echo "infra-static: kubeconform not found (make tools): YAML parse only" >&2
  validate() { uv run --frozen python -c 'import sys, yaml; n = sum(1 for d in yaml.safe_load_all(sys.stdin) if d); print(f"parsed {n} documents")'; }
fi
V=deploy/temporal
step() { printf 'infra-static: %s\n' "$*"; }
step "helm template (plaintext: postgresql + base + kind)"
helm template temporal "$tgz" -n temporal -f $V/values.postgresql.yaml -f $V/values.base.yaml -f $V/values.kind.yaml | validate
step "helm template (mTLS: postgresql + base + tls + kind)"
helm template temporal "$tgz" -n temporal -f $V/values.postgresql.yaml -f $V/values.base.yaml -f $V/values.tls.yaml -f $V/values.kind.yaml | validate
# v0.11: the EKS set (RDS TLS, NLB Service, IRSA SA). The generated IDs file is not in git;
# the committed placeholders render the same objects.
step "helm template (EKS: postgresql + base + tls + eks)"
helm template temporal "$tgz" -n temporal -f $V/values.postgresql.yaml -f $V/values.base.yaml -f $V/values.tls.yaml -f $V/values.eks.yaml | validate
for dir in deploy/k8s/base/* deploy/k8s/overlays/* deploy/k8s/overlays/eks/*; do
  [[ -f "$dir/kustomization.yaml" ]] || continue
  step "kustomize $dir"
  kubectl kustomize "$dir" | validate
done
step "deploy/kind/postgres.yaml"
validate <deploy/kind/postgres.yaml
echo "infra-static: OK"
