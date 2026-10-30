#!/usr/bin/env bash
# Observable acceptance checks for v0.9-temporal-on-kind-mtls (IMPLEMENTATION_PLAN.md v0.9).
# Output is piped through scripts/redact.sh so it can be pasted into the story doc.
#   VERIFY_SKIP_CHECK=1       skip step 1 (make check)
#   VERIFY_SKIP_PLAINTEXT=1   skip part A (commit 1: chart on kind without TLS)
#   VERIFY_SKIP_BEDROCK=1     skip the live step (Finance on Opus 5.5 in kind + Support on Opus 5.5)
#   VERIFY_KEEP_CLUSTER=1     leave the mTLS cluster and port-forwards up at the end
# Part A (plan "commit 1"): fresh kind cluster, chart temporal-1.7.0 / server 1.32.1 with
#   postgresql + base + kind values, plaintext; Finance + payments-mock in the cluster;
#   Support (AgentCore shape, scripted model) on the laptop through a port-forward.
# Part B (plan "commit 2"): a FRESH cluster with postgresql + base + tls + kind in one values
#   set: cert-manager chain, mTLS everywhere, negatives, NetworkPolicy, rotation, Case B (G3).
# Needs: colima context colima-agentcore running, a committed Support tree (build-id.sh),
# nothing on 127.0.0.1:7233 / 8233 / 8080. Uses the scripted models (no AWS) except the
# live step. Stops everything it started; deletes the kind cluster unless VERIFY_KEEP_CLUSTER=1.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
exec > >(scripts/redact.sh) 2>&1

export TEMPORAL_CONFIG_FILE="$PWD/config/temporal.toml"
export KUBECONFIG="$PWD/.temporal/kubeconfig-agentcore-demo"
export DOCKER_CONTEXT="${DOCKER_CONTEXT:-colima-agentcore}"
export AGENTCORE_DEBOUNCE_SECONDS="${AGENTCORE_DEBOUNCE_SECONDS:-20}"
unset TEMPORAL_ADDRESS TEMPORAL_TLS TEMPORAL_NAMESPACE TEMPORAL_TASK_QUEUE TEMPORAL_BUILD_ID \
  FINANCE_APPROVAL_TIMEOUT SUPPORT_NEXUS_SEND_TIMEOUT TEMPORAL_TLS_CLIENT_CERT_PATH TEMPORAL_TLS_CLIENT_KEY_PATH
LOGS=.temporal/verify-v0.9
rm -rf "$LOGS"; mkdir -p "$LOGS"
ROUTE53=temporal-frontend.temporal-customer-support.internal
step() { printf '\n=== %s\n' "$*"; }
fail() { echo "FAIL: $*"; exit 1; }
app_pid=""
stop_app() {
  if [[ -n "$app_pid" ]]; then
    pkill -P "$app_pid" 2>/dev/null || true; kill "$app_pid" 2>/dev/null || true
    wait "$app_pid" 2>/dev/null || true
  fi
  pkill -f 'scripts/scripted_agentcore_app.py' 2>/dev/null || true
  pkill -f 'support_agent.agentcore_app' 2>/dev/null || true
  app_pid=""
}
cleanup() {
  stop_app
  if [[ "${VERIFY_KEEP_CLUSTER:-0}" == "1" ]]; then
    echo "(VERIFY_KEEP_CLUSTER=1: cluster and port-forwards left running; make kind-down to remove)"
  else
    scripts/kind.sh down >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT
k() { kubectl "$@"; }
wait_port() { for _ in $(seq 1 100); do nc -z 127.0.0.1 "$1" 2>/dev/null && return 0; sleep 0.2; done; fail "nothing on :$1"; }
ping_status() { curl -s http://127.0.0.1:8080/ping | jq -r .status; }
wait_ping() { local now=""; for _ in $(seq 1 $(( ${2:-90} * 2 ))); do now="$(ping_status)"; [[ "$now" == "$1" ]] && return 0; sleep 0.5; done; fail "/ping is '$now', expected '$1'"; }
task_state() { temporal workflow query --namespace agent-services --workflow-id "a2a-task-$1" --type get_task -o json 2>/dev/null | jq -r '.queryResult[0].status.state' 2>/dev/null || echo "-"; }
wait_state() { local now="-"; for _ in $(seq 1 $(( ${3:-120} * 2 ))); do now="$(task_state "$1")"; [[ "$now" == "$2" ]] && { echo "a2a-task-$1 is $2"; return 0; }; sleep 0.5; done; fail "a2a-task-$1 is '$now', expected '$2'"; }
artifact() { temporal workflow query --namespace agent-services --workflow-id "a2a-task-$1" --type get_task -o json | jq -c '.queryResult[0].artifacts[0].parts[0].data | {refund_id, approved_by, handled_by}'; }
start_app() {  # the AgentCore-shaped Support app on :8080, scripted model, via make (identity per profile)
  make -s agentcore-local-scripted >>"$LOGS/app-$TEMPORAL_PROFILE.log" 2>&1 & app_pid=$!
  for _ in $(seq 1 120); do curl -sf http://127.0.0.1:8080/ping >/dev/null && break; sleep 0.25; done
  echo "AgentCore-shaped Support app (scripted model) on :8080, profile $TEMPORAL_PROFILE, /ping $(ping_status)"
}
pods_fingerprint() { k get pods -n temporal -l app.kubernetes.io/name=temporal -o jsonpath='{range .items[*]}{.metadata.name} {.status.containerStatuses[0].restartCount}{"\n"}{end}' | sort; }

# The whole conversation set, used by both parts: Case A, then Case B with one Finance pod
# deleted while the task waits for the approver, the Support Worker drained, approve, a
# second invocation finishes it.
run_cases() {  # run_cases <suffix>
  local a="kind-$1-a" b="kind-$1-b" victim
  start_app
  make -s invoke-local | sed 's/^/  /'
  make -s register-version MODE=--poller 2>&1 | tail -1 | sed 's/^/  /'
  echo "-- Case A ($a)"
  make -s case-a-serverless WF="$a" | grep -E 'Status|Result ' | sed 's/^ */  /'
  echo "  refund_confirmation: $(artifact "$a")"
  echo "-- Case B ($b)"
  make -s case-b-serverless WF="$b" | tail -1 | sed 's/^/  /'
  wait_state "$b" input-required 60 | sed 's/^/  /'
  victim="$(k get pods -n temporal-workers -l app=agent-services-worker -o jsonpath='{.items[0].metadata.name}')"
  local t0; t0=$(date +%s)
  k delete pod -n temporal-workers "$victim" >/dev/null
  echo "  deleted Finance pod $victim while the task waits (graceful: $(( $(date +%s) - t0 ))s)"
  wait_ping Healthy 60; echo "  Support Worker drained: /ping Healthy (no Support poller while Finance waits)"
  make -s approve WF="$b" | tail -1 | sed 's/^/  /'
  make -s invoke-local | tail -1 | sed 's/^/  /'
  temporal workflow result --namespace agents --workflow-id "$b" | grep -E 'Status|Result ' | sed 's/^ */  /'
  local art; art="$(artifact "$b")"; echo "  refund_confirmation: $art"
  [[ "$(jq -r .handled_by <<<"$art")" == agent-services-worker-* ]] || fail "handled_by is not a kind pod"
  [[ "$(jq -r .handled_by <<<"$art")" != "$victim" ]] || fail "the deleted pod issued the refund"
  stop_app
}

step "1. make check"
if [[ "${VERIFY_SKIP_CHECK:-0}" == "1" ]]; then echo "SKIPPED (VERIFY_SKIP_CHECK=1)"; else
  make -s check >"$LOGS/check.log" 2>&1 || { cat "$LOGS/check.log"; exit 1; }
  grep -E "passed|0 errors|infra-static: OK|scrub-check|make check" "$LOGS/check.log"
fi

step "2. preflight: tools, docker context, free ports, Build ID"
nc -z 127.0.0.1 7233 2>/dev/null && fail "something listens on 127.0.0.1:7233 (stop the dev server: make dev-server-down)"
nc -z 127.0.0.1 8080 2>/dev/null && fail "something listens on 127.0.0.1:8080"
echo "kind $(kind version | awk '{print $2}'), helm $(helm version --short), kubectl $(kubectl version --client 2>/dev/null | head -1 | awk '{print $3}'), docker context $DOCKER_CONTEXT ($(docker info --format '{{.Architecture}}, {{.NCPU}} CPU'))"
BUILD_ID="$(scripts/build-id.sh)"; echo "Support Build ID $BUILD_ID"
scripts/kind.sh down >/dev/null 2>&1 || true

if [[ "${VERIFY_SKIP_PLAINTEXT:-0}" == "1" ]]; then
  step "A. commit 1 (plaintext): SKIPPED (VERIFY_SKIP_PLAINTEXT=1)"
else
  export TEMPORAL_PROFILE=kind-plaintext KIND_TLS=0
  step "A1. kind-up + kind-temporal KIND_TLS=0 (helm template | kubectl apply --dry-run=server, then install)"
  t0=$(date +%s)
  scripts/kind.sh up >"$LOGS/a-up.log" 2>&1 || { tail -30 "$LOGS/a-up.log"; exit 1; }
  scripts/kind.sh temporal >"$LOGS/a-temporal.log" 2>&1 || { tail -30 "$LOGS/a-temporal.log"; exit 1; }
  grep -E '^\[kind\] (values|dry run|Temporal is up)' "$LOGS/a-temporal.log"
  echo "cluster + server in $(( $(date +%s) - t0 ))s"
  step "A2. Finance + payments-mock in the cluster (scripted model), port-forward, Nexus endpoint"
  FINANCE_MODEL=scripted scripts/kind.sh workers >"$LOGS/a-workers.log" 2>&1 || { tail -30 "$LOGS/a-workers.log"; exit 1; }
  scripts/kind.sh pf | sed 's/^/  /'
  make -s nexus-endpoint | tail -1
  step "A3. Cases A and B over plaintext (Support on the laptop in AgentCore shape)"
  run_cases plain
  step "A4. tear down (commit 2 starts from a fresh cluster)"
  scripts/kind.sh down 2>&1 | tail -1
fi

export TEMPORAL_PROFILE=kind KIND_TLS=1
step "B1. fresh cluster: kind-up + kind-temporal with postgresql + base + tls + kind in ONE values set"
t0=$(date +%s)
scripts/kind.sh up >"$LOGS/b-up.log" 2>&1 || { tail -30 "$LOGS/b-up.log"; exit 1; }
grep -E 'cert-manager|root CA|condition met' "$LOGS/b-up.log" | sed 's/^/  /'
scripts/kind.sh temporal >"$LOGS/b-temporal.log" 2>&1 || { tail -30 "$LOGS/b-temporal.log"; exit 1; }
grep -E '^\[kind\] (values|dry run|certificates|Temporal is up)' "$LOGS/b-temporal.log" | sed 's/^/  /'
echo "cluster + server in $(( $(date +%s) - t0 ))s"

step "B2. pods run 1.32.1 on arm64; shards; namespaces from values (admintools over mTLS)"
k get pods -n temporal -o 'custom-columns=POD:.metadata.name,IMAGE:.spec.containers[0].image,READY:.status.containerStatuses[0].ready'
echo "node arch: $(k get nodes -o jsonpath='{.items[0].status.nodeInfo.architecture}'), kubelet $(k get nodes -o jsonpath='{.items[0].status.nodeInfo.kubeletVersion}')"
k exec deploy/temporal-admintools -n temporal -- temporal operator cluster describe -o json \
  | jq -c '{serverVersion, historyShardCount}'
echo "admintools TLS env: $(k exec deploy/temporal-admintools -n temporal -- env | grep -E '^TEMPORAL_TLS_(SERVER_NAME|CLIENT_CERT_PATH)' | tr '\n' ' ')"
echo "admintools mounts Secret: $(k get deploy/temporal-admintools -n temporal -o jsonpath='{.spec.template.spec.volumes[?(@.name=="client-tls")].secret.secretName}') (the FRONTEND cert, used as a client cert)"
k exec deploy/temporal-admintools -n temporal -- temporal operator namespace list -o json | jq -r '[.[].namespaceInfo.name] | sort | join(" ")'

step "B3. WCI: dynamic config constrained to agents; startup logs of the worker service"
k get cm temporal-dynamic-config -n temporal -o jsonpath='{.data.dynamic_config\.yaml}' | grep -A3 '^workercontroller' | sed 's/^/  /'
k logs deploy/temporal-worker -n temporal | grep '^{' | jq -r 'select(.msg | test("workercontroller")) | "  " + .msg' | head -5
k logs deploy/temporal-worker -n temporal | grep '^{' | jq -r 'select(.msg == "Started Worker" and .TaskQueue == "temporal-sys-per-ns-tq") | "  per-namespace system worker: \(.Namespace)"' | sort -u

step "B4. certificates: cert-manager chain, EKUs, SANs, one CN per identity"
k get certificates -A -o 'custom-columns=NS:.metadata.namespace,NAME:.metadata.name,READY:.status.conditions[0].status,ISSUER:.spec.issuerRef.name'
cert_info() {  # cert_info NS SECRET
  k get secret "$2" -n "$1" -o jsonpath='{.data.tls\.crt}' | base64 -d \
    | openssl x509 -noout -subject -issuer -ext extendedKeyUsage,subjectAltName -nameopt oneline 2>/dev/null \
    | tr '\n' ' ' | sed -E 's/  +/ /g; s/X509v3 //g'
  echo
}
for s in temporal-frontend-tls temporal-internode-tls temporal-web-client-tls temporal-operator-tls agentcore-worker-tls laptop-support-tls; do
  printf '  %-24s %s\n' "$s" "$(cert_info temporal "$s")"
done

step "B5. Finance (2 pods) + payments-mock in the cluster; laptop certs; port-forward; Nexus endpoint"
FINANCE_MODEL=scripted scripts/kind.sh workers >"$LOGS/b-workers.log" 2>&1 || { tail -30 "$LOGS/b-workers.log"; exit 1; }
k get pods -n temporal-workers -o wide | awk '{print "  " $1, $2, $3}'
printf '  %-24s %s\n' eks-nexus-worker-tls "$(cert_info temporal-workers eks-nexus-worker-tls)"
scripts/certs-pull.sh | sed 's/^/  /'
scripts/kind.sh pf | sed 's/^/  /'
make -s nexus-endpoint | tail -1
echo "Finance env: $(k get deploy/agent-services-worker -n temporal-workers -o json | jq -r '.spec.template.spec.containers[0].env[] | select(.name | test("^TEMPORAL_(ADDRESS|TLS_SERVER_NAME)$")) | "\(.name)=\(.value)"' | tr '\n' ' ')"
for _ in $(seq 1 30); do
  pollers="$(temporal task-queue describe -n agent-services --task-queue agent-services-tq -o json | jq -r '[.pollers[]?.identity] | unique | length')"
  [[ "$pollers" -ge 2 ]] && break; sleep 1
done
running="$(k get pods -n temporal-workers -l app=agent-services-worker -o jsonpath='{range .items[*]}1@{.metadata.name}{"\n"}{end}')"
echo "agent-services-tq pollers that are running Finance pods (by service name over mTLS):"
temporal task-queue describe -n agent-services --task-queue agent-services-tq -o json \
  | jq -r '[.pollers[]?.identity] | unique | .[]' | grep -Fx -f <(printf '%s\n' "$running") | sed 's/^/  /'

step "B6. mTLS negatives (CLI through the port-forward; the operator cert is the positive control)"
F="$LOGS/foreign"; mkdir -p "$F"
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout "$F/ca.key" -out "$F/ca.crt" -subj "/CN=foreign root" -days 1 2>/dev/null
openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout "$F/c.key" -out "$F/c.csr" -subj "/CN=operator" 2>/dev/null
printf 'extendedKeyUsage=clientAuth\n' >"$F/ext"
openssl x509 -req -in "$F/c.csr" -CA "$F/ca.crt" -CAkey "$F/ca.key" -CAcreateserial -out "$F/c.crt" -days 1 -extfile "$F/ext" 2>/dev/null
neg() {  # neg LABEL CLI-ARGS...   (no profile: only the flags given)
  local label="$1"; shift; wait_port 7233
  if out="$(env -u TEMPORAL_PROFILE temporal operator namespace list --address 127.0.0.1:7233 "$@" 2>&1)"; then fail "$label was accepted"; fi
  echo "  rejected  $label: $(grep -m1 -E 'Error' <<<"$out" | sed 's/.*127.0.0.1:7233: //' | cut -c1-140)"
  sleep 1
}
neg "plaintext client"
neg "TLS, no client cert" --tls --tls-ca-path .certs/ca.crt --tls-server-name "$ROUTE53"
neg "client cert from a foreign CA (CN=operator)" --tls-ca-path .certs/ca.crt --tls-server-name "$ROUTE53" --tls-cert-path "$F/c.crt" --tls-key-path "$F/c.key"
neg "operator cert, wrong server name" --tls-ca-path .certs/ca.crt --tls-server-name evil.example.com --tls-cert-path .certs/operator/tls.crt --tls-key-path .certs/operator/tls.key
wait_port 7233
echo "  accepted  operator cert, Route 53 name (profile kind): $(temporal operator namespace list -o json | jq -r '[.[].namespaceInfo.name] | sort | join(" ")')"
wait_port 8233
echo "  Web UI through web.tls (client cert CN temporal-ui): $(curl -sS http://127.0.0.1:8233/api/v1/namespaces | jq -r '[.namespaces[].namespaceInfo.name] | sort | join(" ")')"

step "B7. Cases A and B over mTLS (G3): Support = agentcore-worker cert via the Route 53 name; Finance = eks-nexus-worker by service name"
run_cases mtls
echo "(G3: Case B over mTLS passed)"

step "B8. Support's identity on kind: its own client cert, no dev-server TLS/address env"
start_app
sleep 1
pid="$(pgrep -f 'scripts/scripted_agentcore_app.py' | head -1)"
ps eww "$pid" | tr ' ' '\n' | grep -E '^TEMPORAL_(PROFILE|TLS|ADDRESS)' | sed "s#$PWD/##; s/^/  /"
openssl x509 -in .certs/agentcore-worker/tls.crt -noout -subject -nameopt oneline | sed 's/^/  agentcore-worker cert: /'
stop_app

step "B9. NetworkPolicy (kindnet): who can reach the frontend on 7233"
probe() {  # probe NAMESPACE [--labels=...]
  k run "np-probe-$RANDOM" -n "$1" --image=busybox:1.37 --restart=Never --rm -i --quiet ${2:+"$2"} --command -- \
    sh -c 'if nc -z -w 3 temporal-frontend.temporal.svc.cluster.local 7233; then echo REACHABLE; else echo BLOCKED; fi' 2>/dev/null | tail -1
}
r1="$(probe temporal-workers)"; echo "  unlabelled pod in temporal-workers -> 7233: $r1"
r2="$(probe default)"; echo "  unlabelled pod in default          -> 7233: $r2"
r3="$(probe temporal-workers --labels=temporal.io/client=true)"; echo "  pod labelled temporal.io/client=true -> 7233: $r3"
[[ "$r1" == BLOCKED && "$r2" == BLOCKED && "$r3" == REACHABLE ]] || fail "NetworkPolicy not enforced as designed"
fpod="$(k get pods -n temporal-workers -l app=agent-services-worker -o jsonpath='{.items[0].metadata.name}')"
pycheck='import socket,sys
s=socket.socket(); s.settimeout(3)
try:
    s.connect((sys.argv[1], int(sys.argv[2]))); print("REACHABLE")
except Exception as e:
    print("BLOCKED", type(e).__name__)'
echo "  Finance pod egress -> postgres.temporal:5432:  $(k exec -n temporal-workers "$fpod" -- python -c "$pycheck" postgres.temporal.svc.cluster.local 5432)"
echo "  Finance pod egress -> payments-mock:8080:      $(k exec -n temporal-workers "$fpod" -- python -c "$pycheck" payments-mock.temporal-workers.svc.cluster.local 8080)"

step "B10. dynamic-config-only helm upgrade: takes effect, no pod restarts (probe: standalone Nexus off)"
before="$(pods_fingerprint)"
wait_port 7233; make -s card >/dev/null 2>&1 && echo "  make card (standalone Nexus get_agent_card, Q16 on 1.32.1): works"
DC_VALUES=deploy/temporal/probes/dc-standalone-nexus-off.yaml scripts/kind.sh dc-upgrade | sed 's/^/  /'
t0=$(date +%s); ok=0
for _ in $(seq 1 40); do
  if out="$(make -s card 2>&1)"; then sleep 3; else ok=1; break; fi
done
[[ $ok == 1 ]] || fail "dynamic config change never took effect"
echo "  refused after $(( $(date +%s) - t0 ))s: $(grep -m1 -o 'Standalone Nexus operation is disabled' <<<"$out")"
k get cm temporal-dynamic-config -n temporal -o jsonpath='{.data.dynamic_config\.yaml}' | grep -A1 enableStandalone | sed 's/^/  configmap: /'
scripts/kind.sh temporal >"$LOGS/b-revert.log" 2>&1; t0=$(date +%s)
for _ in $(seq 1 40); do make -s card >/dev/null 2>&1 && break; sleep 3; done
echo "  reverted (normal values): make card works again after $(( $(date +%s) - t0 ))s"
after="$(pods_fingerprint)"
[[ "$before" == "$after" ]] && echo "  same pods, 0 restarts before and after both upgrades:" || fail "pods changed: $before -> $after"
sed 's/^/    /' <<<"$after"

step "B11. rotation: server cert hot-reload (Q10) and the SDK's file certs (Q18)"
serial() { wait_port 7233; openssl s_client -connect 127.0.0.1:7233 -servername "$ROUTE53" -cert .certs/operator/tls.crt -key .certs/operator/tls.key -CAfile .certs/ca.crt </dev/null 2>/dev/null | openssl x509 -noout -serial | cut -d= -f2; }
fe_pod() { k get pod -n temporal -l app.kubernetes.io/component=frontend -o jsonpath='{.items[0].metadata.name} restarts={.items[0].status.containerStatuses[0].restartCount}'; }
s0="$(serial)"; p0="$(fe_pod)"
echo "  frontend serves serial $s0 ($p0)"
scripts/cert-renew.sh temporal temporal-frontend-tls | sed 's/^/  /'; t0=$(date +%s)
s1="$s0"; for _ in $(seq 1 60); do s1="$(serial)"; [[ -n "$s1" && "$s1" != "$s0" ]] && break; sleep 5; done
[[ "$s1" != "$s0" ]] || fail "frontend kept serving the old certificate"
echo "  frontend serves serial $s1 after $(( $(date +%s) - t0 ))s ($(fe_pod)): hot-reloaded, no restart (refreshInterval 1m)"
[[ "$(fe_pod)" == "$p0" ]] || fail "frontend pod changed"
fser() { k exec -n temporal-workers "$fpod" -- python -c "import ssl; print(ssl._ssl._test_decode_cert('/etc/temporal/tls/tls.crt')['serialNumber'])"; }
fpod="$(k get pods -n temporal-workers -l app=agent-services-worker -o jsonpath='{.items[0].metadata.name}')"
f0="$(fser)"; scripts/cert-renew.sh temporal-workers eks-nexus-worker-tls | sed 's/^/  /'; t0=$(date +%s)
f1="$f0"; for _ in $(seq 1 40); do f1="$(fser)"; [[ "$f1" != "$f0" ]] && break; sleep 5; done
echo "  Finance pod $fpod: mounted tls.crt serial $f0 -> $f1 after $(( $(date +%s) - t0 ))s (kubelet sync; restarts=$(k get pod -n temporal-workers "$fpod" -o jsonpath='{.status.containerStatuses[0].restartCount}'))"
echo "  The running Worker keeps the cert it read at connect (TLSConfig bytes; Q18): the old cert is still valid,"
echo "  so it keeps working; new pods present the new one. Fallback: rollout restart (or Reloader) after renewal."
start_app; make -s invoke-local >/dev/null
make -s case-a-serverless WF=kind-mtls-after-rotation | grep -E 'Status' | sed 's/^ */  after both renewals, Case A: /'
stop_app
k rollout restart deploy/agent-services-worker -n temporal-workers >/dev/null
k rollout status deploy/agent-services-worker -n temporal-workers --timeout=180s | tail -1 | sed 's/^/  /'

step "C. live: Finance on Opus 5.5 in kind (kind-aws-creds) + Support on Opus 5.5 (agentcore-local)"
if [[ "${VERIFY_SKIP_BEDROCK:-0}" == "1" ]]; then echo "SKIPPED (VERIFY_SKIP_BEDROCK=1)"
elif ! aws sts get-caller-identity >/dev/null 2>&1; then echo "SKIPPED: no valid AWS session (aws sso login)"
else
  scripts/kind-aws-creds.sh | sed 's/^/  /'
  FINANCE_MODEL=bedrock scripts/kind.sh workers >"$LOGS/c-workers.log" 2>&1 || { tail -20 "$LOGS/c-workers.log"; exit 1; }
  make -s agentcore-local >>"$LOGS/app-live.log" 2>&1 & app_pid=$!
  for _ in $(seq 1 120); do curl -sf http://127.0.0.1:8080/ping >/dev/null && break; sleep 0.25; done
  make -s invoke-local >/dev/null
  make -s case-a-serverless WF=kind-live-a | grep -E 'Status|Result ' | sed 's/^ */  /'
  echo "  refund_confirmation: $(artifact kind-live-a)"
  stop_app
fi

step "D. teardown"
if [[ "${VERIFY_KEEP_CLUSTER:-0}" != "1" ]]; then
  stop_app; scripts/kind.sh down 2>&1 | tail -1
fi
echo
echo "VERIFY OK"
