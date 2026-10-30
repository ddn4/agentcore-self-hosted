#!/usr/bin/env bash
# The real Temporal server on a local kind cluster (plan v0.9; runbook Phase 2, 3, 7b).
#   scripts/kind.sh up          kind cluster + cert-manager + root CA/temporal-ca + postgres  (make kind-up)
#   scripts/kind.sh temporal    chart temporal-1.7.0, images 1.32.1; KIND_TLS=1 (default) adds
#                               values.tls.yaml + the cert-manager certificates          (make kind-temporal)
#   scripts/kind.sh images      build the Finance + payments-mock images, load into kind  (make kind-images)
#   scripts/kind.sh workers     Finance (2 pods) + payments-mock + NetworkPolicies        (make kind-workers)
#                               FINANCE_MODEL=scripted (default without AWS creds) | bedrock
#   scripts/kind.sh aws-creds   short-lived laptop credentials -> Secret aws-creds        (make kind-aws-creds)
#   scripts/kind.sh pf          port-forward frontend :7233 and UI :8233 in the background (make pf)
#   scripts/kind.sh pf-stop     stop the port-forwards                                     (make pf-stop)
#   scripts/kind.sh dc-upgrade  helm upgrade with the same values + DC_VALUES file (dynamic-config-only change)
#   scripts/kind.sh status      nodes, pods, certificates
#   scripts/kind.sh down        stop port-forwards, delete the cluster                     (make kind-down)
# Docker: always DOCKER_CONTEXT (default colima-agentcore); the global context is never changed.
# kubectl/helm/kind: a project-local kubeconfig (.temporal/kubeconfig-agentcore-demo, gitignored),
# so ~/.kube/config and the user's current context are never touched.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

export DOCKER_CONTEXT="${DOCKER_CONTEXT:-colima-agentcore}"
CLUSTER=agentcore-demo
KCTX="kind-$CLUSTER"
CHART_REPO=https://go.temporal.io/helm-charts
CHART_VERSION=1.7.0
CERT_MANAGER_VERSION=v1.21.3
KIND_TLS="${KIND_TLS:-1}"
PIDS=.temporal/kind-pf
mkdir -p .temporal
export KUBECONFIG="$PWD/.temporal/kubeconfig-$CLUSTER"
k() { kubectl --context "$KCTX" "$@"; }
h() { helm --kube-context "$KCTX" "$@"; }
log() { printf '[kind] %s\n' "$*"; }

# Sets the global array VALS (bash 3.2 on macOS: no mapfile).
values_args() {
  VALS=(-f deploy/temporal/values.postgresql.yaml -f deploy/temporal/values.base.yaml)
  if [[ "$KIND_TLS" == "1" ]]; then VALS+=(-f deploy/temporal/values.tls.yaml); fi
  VALS+=(-f deploy/temporal/values.kind.yaml)
}

wait_certs() {  # wait_certs NAMESPACE
  k wait --for=condition=Ready certificate --all -n "$1" --timeout=120s
}

cmd_up() {
  if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
    log "cluster $CLUSTER exists"
  else
    log "creating cluster $CLUSTER (docker context $DOCKER_CONTEXT)"
    kind create cluster --config deploy/kind/kind-config.yaml --wait 120s
  fi
  log "cert-manager $CERT_MANAGER_VERSION (runbook 2.6 step 1)"
  h upgrade --install cert-manager cert-manager --repo https://charts.jetstack.io \
    --version "$CERT_MANAGER_VERSION" -n cert-manager --create-namespace \
    --set crds.enabled=true --wait --timeout 300s >/dev/null
  log "root CA + temporal-ca ClusterIssuer (runbook 2.6 step 2)"
  k apply -k deploy/k8s/base/cert-issuers
  k wait --for=condition=Ready certificate/temporal-root-ca -n cert-manager --timeout=120s
  k wait --for=condition=Ready clusterissuer/temporal-ca --timeout=120s
  k create namespace temporal --dry-run=client -o yaml | k apply -f -
  if ! k get secret temporal-db-secret -n temporal >/dev/null 2>&1; then
    log "random PostgreSQL password -> Secret temporal/temporal-db-secret"
    k create secret generic temporal-db-secret -n temporal \
      --from-literal=password="$(openssl rand -hex 24)" >/dev/null
  fi
  k apply -f deploy/kind/postgres.yaml
  k rollout status deploy/postgres -n temporal --timeout=180s
}

cmd_temporal() {
  values_args
  if [[ "$KIND_TLS" == "1" ]]; then
    log "certificates (runbook 2.6 steps 3-4)"
    k apply -k deploy/k8s/base/temporal-certs
    wait_certs temporal
  fi
  log "values: ${VALS[*]}"
  log "dry run: helm template | kubectl apply --dry-run=server (runbook 2.11 step 2)"
  h template temporal temporal --repo "$CHART_REPO" --version "$CHART_VERSION" \
    "${VALS[@]}" --namespace temporal | k apply --dry-run=server -n temporal -f - >/dev/null
  log "helm upgrade --install temporal (chart $CHART_VERSION)"
  h upgrade --install temporal temporal --repo "$CHART_REPO" --version "$CHART_VERSION" \
    "${VALS[@]}" --namespace temporal --wait --timeout 900s
  for d in frontend history matching worker web admintools; do
    k rollout status "deploy/temporal-$d" -n temporal --timeout=300s >/dev/null
  done
  log "Temporal is up (TLS=$KIND_TLS)"
}

cmd_dc_upgrade() {
  [[ -n "${DC_VALUES:-}" ]] || { echo "DC_VALUES=<extra values file> required" >&2; exit 2; }
  values_args
  h upgrade temporal temporal --repo "$CHART_REPO" --version "$CHART_VERSION" \
    "${VALS[@]}" -f "$DC_VALUES" --namespace temporal --wait --timeout 300s >/dev/null
  log "upgraded with $DC_VALUES"
}

cmd_images() {
  log "building images (docker context $DOCKER_CONTEXT)"
  docker build -q -f services/finance-agent/Dockerfile --target finance -t agentcore-demo/finance-agent:kind . >/dev/null
  docker build -q -f services/finance-agent/Dockerfile --target finance-scripted -t agentcore-demo/finance-agent:kind-scripted . >/dev/null
  docker build -q -f services/payments-mock/Dockerfile -t agentcore-demo/payments-mock:kind . >/dev/null
  for img in agentcore-demo/finance-agent:kind agentcore-demo/finance-agent:kind-scripted agentcore-demo/payments-mock:kind; do
    kind load docker-image --name "$CLUSTER" "$img" >/dev/null
  done
  log "images loaded into $CLUSTER"
}

finance_model() {
  if [[ -n "${FINANCE_MODEL:-}" ]]; then echo "$FINANCE_MODEL"
  elif k get secret aws-creds -n temporal-workers >/dev/null 2>&1; then echo bedrock
  else echo scripted; fi
}

cmd_workers() {
  local model overlay; model="$(finance_model)"
  case "$model" in
    scripted) overlay=deploy/k8s/overlays/kind-scripted ;;
    bedrock)  overlay=deploy/k8s/overlays/kind ;;
    *) echo "FINANCE_MODEL must be scripted or bedrock" >&2; exit 2 ;;
  esac
  cmd_images
  log "applying $overlay (Finance model: $model)"
  k apply -k "$overlay"
  wait_certs temporal-workers
  if [[ "$KIND_TLS" != "1" ]]; then
    log "plaintext server: TEMPORAL_TLS=false on the Finance Deployment"
    k set env deploy/agent-services-worker -n temporal-workers TEMPORAL_TLS=false >/dev/null
  fi
  k rollout restart deploy/agent-services-worker -n temporal-workers >/dev/null
  k rollout status deploy/payments-mock -n temporal-workers --timeout=180s
  k rollout status deploy/agent-services-worker -n temporal-workers --timeout=300s
  # Pods of the first ReplicaSet may still be starting when the restart above lands (their
  # Secret is issued a second after the apply) and then linger in Terminating while
  # polling. Wait until only the current ReplicaSet's pods remain.
  local want n
  want="$(k get deploy/agent-services-worker -n temporal-workers -o jsonpath='{.spec.replicas}')"
  for _ in $(seq 1 60); do
    n="$(k get pods -n temporal-workers -l app=agent-services-worker --no-headers 2>/dev/null | wc -l | tr -d ' ')"
    [[ "$n" == "$want" ]] && break
    sleep 1
  done
  if [[ "$n" != "$want" ]]; then
    log "forcing out lingering Finance pods"
    k get pods -n temporal-workers -l app=agent-services-worker -o json \
      | jq -r '.items[] | select(.metadata.deletionTimestamp != null) | .metadata.name' \
      | while read -r pod; do k delete pod -n temporal-workers "$pod" --grace-period=0 --force >/dev/null 2>&1 || true; done
  fi
}

cmd_aws_creds() {
  # Plan 3.2: on kind, `aws configure export-credentials` -> a short-lived k8s Secret.
  # Never committed, never in .env; expires with the SSO session.
  local env
  env="$(aws configure export-credentials --format env-no-export)" || {
    echo "kind-aws-creds: no AWS credentials (aws sso login); Finance stays scripted" >&2; exit 1; }
  k create namespace temporal-workers --dry-run=client -o yaml | k apply -f - >/dev/null
  # shellcheck disable=SC2046
  k create secret generic aws-creds -n temporal-workers --dry-run=client -o yaml \
    $(printf '%s\n' "$env" | sed -n 's/^\(AWS_[A-Z_]*\)=\(.*\)$/--from-literal=\1=\2/p') | k apply -f - >/dev/null
  log "Secret temporal-workers/aws-creds updated (expires with the SSO session)"
}

pf_one() {  # pf_one NAME SVC LOCAL REMOTE
  # kubectl port-forward exits ("lost connection to pod") when a forwarded connection is
  # reset, which every rejected mTLS handshake does (the negative tests). A small
  # supervisor loop restarts it, so the laptop path stays up for the whole demo.
  local pidf="$PIDS-$1.pid"
  if [[ -f "$pidf" ]] && kill -0 "$(cat "$pidf")" 2>/dev/null; then log "pf $1 already running"; return; fi
  nohup bash -c 'while true; do
      kubectl port-forward -n temporal "svc/$1" "$2:$3" --address 127.0.0.1
      sleep 0.3
    done' pf "$2" "$3" "$4" >".temporal/kind-pf-$1.log" 2>&1 &
  echo $! >"$pidf"
  for _ in $(seq 1 50); do nc -z 127.0.0.1 "$3" 2>/dev/null && { log "pf $1: 127.0.0.1:$3 -> svc/$2:$4"; return; }; sleep 0.2; done
  echo "pf $1 did not come up; see .temporal/kind-pf-$1.log" >&2; exit 1
}

cmd_pf() {
  pf_one frontend temporal-frontend 7233 7233
  pf_one web temporal-web 8233 8080
}

cmd_pf_stop() {
  local pid
  for f in "$PIDS"-*.pid; do
    [[ -f "$f" ]] || continue
    pid="$(cat "$f")"
    kill "$pid" 2>/dev/null || true          # the supervisor loop first, then its kubectl
    pkill -P "$pid" 2>/dev/null || true
    rm -f "$f"
  done
  log "port-forwards stopped"
}

cmd_status() {
  k get nodes -o wide
  k get pods -A -o wide | grep -Ev 'kube-system|local-path' || true
  k get certificates -A || true
}

cmd_down() {
  cmd_pf_stop
  if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
    kind delete cluster --name "$CLUSTER"
  else
    log "no cluster $CLUSTER"
  fi
}

case "${1:-}" in
  up) cmd_up ;;
  temporal) cmd_temporal ;;
  dc-upgrade) cmd_dc_upgrade ;;
  images) cmd_images ;;
  workers) cmd_workers ;;
  aws-creds) cmd_aws_creds ;;
  pf) cmd_pf ;;
  pf-stop) cmd_pf_stop ;;
  status) cmd_status ;;
  down) cmd_down ;;
  *) sed -n '2,17p' "$0"; exit 2 ;;
esac
