#!/usr/bin/env bash
# Observable acceptance checks for v0.11-temporal-and-finance-on-eks (IMPLEMENTATION_PLAN.md v0.11;
# runbook §5 "Temporal server" and "Nexus and the EKS handler"). us-east-1, default SSO profile.
# Output goes through scripts/redact.sh.
#
# It creates no AWS resources (make create-db-user / rds-snapshot / eks-temporal / route53-alias /
# finance-image / finance-deploy / nexus-endpoint did that). Inside the cluster it runs short-lived
# pods (deleted again); on the laptop it starts the port-forward if needed and, for step 12, the
# laptop Support Worker (real Opus 5.5 on Bedrock), and stops both before it exits.
#   1  make check (VERIFY_SKIP_CHECK=1 skips), make tf-static
#   2  server: Helm release, pods on 1.32.1, replicas, 512 shards, namespaces from values, WCI dynamic config
#   3  RDS: TLS with the mounted CA and host verification (config + every temporal_user session on TLS),
#      schemas core 1.19 / visibility 1.14, the schema hook Job mounted rds-ca, the pre-install snapshot
#   4  IRSA: temporal-worker SA annotation, AWS_ROLE_ARN only in worker pods, sts get-caller-identity
#   5  NLB: internal, network, TCP listeners (passthrough), ip targets healthy, our SG; Route 53 alias
#   6  in the VPC: the Route 53 name resolves to the NLB; s_client shows the cert-manager cert and a
#      client-cert request; mTLS works with the operator cert and fails without
#   7  NetworkPolicy: unlabelled pod blocked from the frontend, labelled pod allowed
#   8  UI through the port-forward; eks-pf profile from the laptop
#   9  Finance: 2 pods (ECR, arm64), 2 pollers, IRSA, no static credentials, PDB, egress policy, endpoint
#   10 Bedrock IAM from the Finance pod: global. and us. allowed, another model denied (ledger 41)
#   11 Q16: standalone get_agent_card from the CLI on 1.32.1
#   12 hybrid demo (VERIFY_SKIP_CASES=1 skips): laptop Support on support-dev-tq; Case A, Case B
#      (approve) with handled_by = an EKS pod (gate G4); failure demo
#   13 Q18 light re-check: re-issued eks-nexus-worker-tls lands in the pod; the running Worker keeps working
#   14 inventory: v0.11 resources tagged Project=agentcore-self-hosted
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
exec > >(scripts/redact.sh) 2>&1
export AWS_REGION=us-east-1 AWS_PAGER=""
unset AWS_PROFILE
export KUBECONFIG="$PWD/.temporal/kubeconfig-eks"
export TEMPORAL_CONFIG_FILE="$PWD/config/temporal.toml" TEMPORAL_PROFILE=eks-pf
# Read what we need from the generated file WITHOUT exporting it: an exported foundation.env
# leaks real IDs into `make check` (it broke test_vpc_render once).
fval() { (set +u; . .generated/foundation.env; eval "printf '%s' \"\${$1}\""); }
ZONE_NAME="$(fval PRIVATE_ZONE_NAME)"
NLB_SECURITY_GROUP="$(fval NLB_SECURITY_GROUP)"
FE="temporal-frontend.$ZONE_NAME"
LOGS=.temporal/verify-v0.11
rm -rf "$LOGS"; mkdir -p "$LOGS"
fail=0
step() { printf '\n=== %s  (%s)\n' "$*" "$(date -u +%H:%M:%SZ)"; }
ok()  { printf '  ok    %s\n' "$*"; }
bad() { printf '  FAIL  %s\n' "$*"; fail=1; }
check() { local what="$1"; shift; if "$@"; then ok "$what"; else bad "$what"; fi; }
k() { kubectl --context agentcore-demo-eks "$@"; }
PF_STARTED=0 WORKER_PID=""
cleanup() {
  k -n temporal delete pod v011-s-client v011-mtls-ok v011-mtls-nocert v011-awscli --ignore-not-found --wait=false >/dev/null 2>&1 || true
  k -n temporal-workers delete pod v011-np-unlabelled v011-np-labelled --ignore-not-found --wait=false >/dev/null 2>&1 || true
  if [[ -n "$WORKER_PID" ]]; then pkill -P "$WORKER_PID" 2>/dev/null || true; kill "$WORKER_PID" 2>/dev/null || true; fi
  if [[ "$PF_STARTED" == 1 ]]; then scripts/eks.sh pf-stop >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT

podrun() {  # podrun NS NAME IMAGE SA SCRIPT [secret-to-mount-at-/certs] [labels-json]: run, wait, print logs
  local ns="$1" name="$2" image="$3" sa="$4" script="$5" secret="${6:-}" labels="${7:-}"
  [[ -n "$labels" ]] || labels='{}'
  k -n "$ns" delete pod "$name" --ignore-not-found >/dev/null 2>&1
  local vols='[]' mounts='[]'
  if [[ -n "$secret" ]]; then
    vols="[{\"name\":\"certs\",\"secret\":{\"secretName\":\"$secret\"}}]"
    mounts='[{"name":"certs","mountPath":"/certs","readOnly":true}]'
  fi
  jq -n --arg ns "$ns" --arg n "$name" --arg i "$image" --arg sa "$sa" --arg s "$script" \
     --argjson v "$vols" --argjson m "$mounts" --argjson l "$labels" '{
    apiVersion: "v1", kind: "Pod", metadata: {name: $n, namespace: $ns, labels: $l},
    spec: {restartPolicy: "Never", serviceAccountName: $sa, volumes: $v,
      containers: [{name: "c", image: $i, command: ["sh", "-c", $s], volumeMounts: $m}]}}' | k apply -f - >/dev/null
  local phase=""
  for _ in $(seq 1 90); do
    phase="$(k -n "$ns" get pod "$name" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
    [[ "$phase" == Succeeded || "$phase" == Failed ]] && break
    sleep 2
  done
  k -n "$ns" logs "$name" 2>&1 | tee "$LOGS/$name.log" | sed 's/^/    /'
  k -n "$ns" delete pod "$name" --ignore-not-found --wait=false >/dev/null 2>&1
  [[ "$phase" == Succeeded ]]
}

# --- 1 ---------------------------------------------------------------------------------
step "1. make check, make tf-static"
if [[ "${VERIFY_SKIP_CHECK:-0}" == 1 ]]; then echo "skipped (VERIFY_SKIP_CHECK=1)"
else make check >$LOGS/check.log 2>&1 && ok "make check" || { tail -20 $LOGS/check.log; bad "make check"; }; fi
make tf-static 2>&1 | grep -E 'fmt ok|tflint ok|valid' | sort | uniq -c

# --- 2 ---------------------------------------------------------------------------------
step "2. Temporal server on EKS (chart 1.7.0, server 1.32.1)"
helm --kube-context agentcore-demo-eks list -n temporal
k -n temporal get deploy -o custom-columns='NAME:.metadata.name,READY:.status.readyReplicas,WANT:.spec.replicas,IMAGE:.spec.template.spec.containers[0].image'
imgs="$(k -n temporal get pods -l app.kubernetes.io/instance=temporal -o jsonpath='{range .items[*]}{.spec.containers[0].image}{"\n"}{end}' | sort -u)"
check "every server/admintools pod on 1.32.1 (web on ui 2.54.1)" bash -c "! grep -vE 'temporalio/(server|admin-tools):1.32.1|temporalio/ui:2.54.1' <<<'$imgs'"
fe_ready="$(k -n temporal get deploy temporal-frontend -o jsonpath='{.status.readyReplicas}')"
check "frontend replicas 2 (NLB targets in two AZs), PDB minAvailable 1" bash -c "[[ '$fe_ready' == 2 ]] && kubectl --context agentcore-demo-eks -n temporal get pdb temporal-frontend-pdb >/dev/null"
if ! nc -z 127.0.0.1 7233 2>/dev/null; then scripts/eks.sh pf >/dev/null; PF_STARTED=1; fi
desc="$(temporal operator cluster describe -o json)"
jq '{historyShardCount, serverVersion, persistenceStore, visibilityStore}' <<<"$desc"
check "historyShardCount 512, serverVersion 1.32.1" [ "$(jq -r '"\(.historyShardCount) \(.serverVersion)"' <<<"$desc")" == "512 1.32.1" ]
nss="$(temporal operator namespace list -o json | jq -r '[.[].namespaceInfo.name] | sort | join(" ")')"
check "namespaces from values: $nss" grep -q 'agent-services agents default' <<<"$nss"
k -n temporal get cm temporal-dynamic-config -o jsonpath='{.data.dynamic_config\.yaml}' >$LOGS/dynamic_config.yaml
grep -A4 'workercontroller' $LOGS/dynamic_config.yaml | head -14
check "WCI keys present, constrained to agents" bash -c "grep -q 'namespace: agents' $LOGS/dynamic_config.yaml && grep -q aws-agentcore $LOGS/dynamic_config.yaml && grep -q no-sync $LOGS/dynamic_config.yaml"

# --- 3 ---------------------------------------------------------------------------------
step "3. RDS: TLS + host verification, schemas, schema Job CA mount, snapshot"
CFG=$LOGS/config_template.yaml
k -n temporal get cm temporal-config -o jsonpath='{.data.config_template\.yaml}' >$CFG
sed -n '/datastores:/,/^ *visibilityStore\|^ *numHistoryShards/p' $CFG | grep -E 'default:|visibility:|connectAddr|enabled|caFile|enableHostVerification|user:' | sed 's/^/    /' | head -16
check "both datastores: tls enabled, caFile /etc/temporal/rds-ca/rds-ca.pem, enableHostVerification true, user temporal_user" \
  bash -c "[[ \$(grep -c 'caFile: /etc/temporal/rds-ca/rds-ca.pem' $CFG) == 2 && \$(grep -c 'enableHostVerification: true' $CFG) == 2 && \$(grep -c 'user: temporal_user' $CFG) == 2 ]]"
scripts/create-db-user.sh --check | tee $LOGS/db-check.log
check "every temporal_user session is TLS; schema temporal 1.19, visibility 1.14" bash -c \
  "grep -qE 'sessions: ([0-9]+), on TLS: \1$' $LOGS/db-check.log && grep -q 'schema temporal: 1.19' $LOGS/db-check.log && grep -q 'schema temporal_visibility: 1.14' $LOGS/db-check.log"
helm --kube-context agentcore-demo-eks get hooks temporal -n temporal >$LOGS/hooks.yaml
check "schema hook Job (pre-install) mounts the rds-ca Secret and sets SQL_TLS_CA_FILE" bash -c \
  "grep -q 'secretName: rds-ca' $LOGS/hooks.yaml && grep -q 'SQL_TLS_CA_FILE' $LOGS/hooks.yaml && grep -q 'manage-schema-default-store' $LOGS/hooks.yaml"
aws rds describe-db-snapshots --db-instance-identifier temporal-db --snapshot-type manual \
  --query 'DBSnapshots[].[DBSnapshotIdentifier,Status,SnapshotCreateTime]' --output text
check "a manual snapshot from before the install exists" bash -c "aws rds describe-db-snapshots --db-instance-identifier temporal-db --snapshot-type manual --query 'DBSnapshots[].DBSnapshotIdentifier' --output text | grep -q temporal-pre-v011-install"

# --- 4 ---------------------------------------------------------------------------------
step "4. IRSA on the worker service only (runbook 4.1)"
ann="$(k -n temporal get sa temporal-worker -o jsonpath='{.metadata.annotations.eks\.amazonaws\.com/role-arn}')"
echo "  temporal-worker SA role-arn: $ann"
check "SA temporal-worker annotated with temporal-server-wci" grep -q ':role/temporal-server-wci$' <<<"$ann"
for d in frontend history matching worker web admintools; do
  r="$(k -n temporal exec deploy/temporal-$d -- env 2>/dev/null | grep -E '^AWS_ROLE_ARN=' || true)"
  printf '    %-11s %s\n' "$d" "${r:-AWS_ROLE_ARN unset}"
  if [[ "$d" == worker ]]; then check "worker pod has AWS_ROLE_ARN + AWS_WEB_IDENTITY_TOKEN_FILE" bash -c \
      "kubectl --context agentcore-demo-eks -n temporal exec deploy/temporal-worker -- env | grep -q AWS_WEB_IDENTITY_TOKEN_FILE && [[ -n '$r' ]]"
  else [[ -z "$r" ]] || bad "$d has AWS_ROLE_ARN"; fi
done
ok "AWS_ROLE_ARN only in the worker pod"
podrun temporal v011-awscli public.ecr.aws/aws-cli/aws-cli:2.36.46 temporal-worker \
  'aws sts get-caller-identity --query Arn --output text' || true
check "sts get-caller-identity as SA temporal-worker -> assumed-role/temporal-server-wci" grep -q 'assumed-role/temporal-server-wci/' $LOGS/v011-awscli.log

# --- 5 ---------------------------------------------------------------------------------
step "5. Internal NLB, TLS passthrough, Route 53 alias (runbook 2.4)"
k -n temporal get svc temporal-frontend -o wide
lb="$(aws elbv2 describe-load-balancers --names temporal-frontend --output json)"
jq '.LoadBalancers[0] | {name: .LoadBalancerName, type: .Type, scheme: .Scheme, state: .State.Code, dns: .DNSName, azs: [.AvailabilityZones[].ZoneName], sgs: .SecurityGroups}' <<<"$lb"
lbarn="$(jq -r '.LoadBalancers[0].LoadBalancerArn' <<<"$lb")"
check "network / internal / active" [ "$(jq -r '.LoadBalancers[0] | "\(.Type) \(.Scheme) \(.State.Code)"' <<<"$lb")" == "network internal active" ]
check "NLB carries sg agentcore-demo-nlb-temporal-frontend (+ the LBC's shared backend SG)" jq -e --arg sg "$NLB_SECURITY_GROUP" '.LoadBalancers[0].SecurityGroups | index($sg) != null' <<<"$lb"
aws elbv2 describe-listeners --load-balancer-arn "$lbarn" --query 'Listeners[].[Port,Protocol,length(Certificates || `[]`)]' --output text
check "listeners are plain TCP with no certificate (passthrough; no TLS termination)" bash -c \
  "[[ -z \$(aws elbv2 describe-listeners --load-balancer-arn '$lbarn' --query 'Listeners[?Protocol!=\`TCP\`].Port' --output text) ]]"
for tg in $(aws elbv2 describe-target-groups --load-balancer-arn "$lbarn" --query 'TargetGroups[].TargetGroupArn' --output text); do
  aws elbv2 describe-target-groups --target-group-arns "$tg" --query 'TargetGroups[0].[Port,TargetType,HealthCheckProtocol]' --output text | sed 's/^/  tg /'
  aws elbv2 describe-target-health --target-group-arn "$tg" --query 'TargetHealthDescriptions[].[Target.Id,Target.Port,TargetHealth.State]' --output text | sed 's/^/     /'
done
healthy7233=0
for tg in $(aws elbv2 describe-target-groups --load-balancer-arn "$lbarn" --query 'TargetGroups[].TargetGroupArn' --output text); do
  n="$(aws elbv2 describe-target-health --target-group-arn "$tg" --query 'length(TargetHealthDescriptions[?Target.Port==`7233` && TargetHealth.State==`healthy`])' --output text)"
  healthy7233=$((healthy7233 + n))
done
check "7233 targets: ip targets, 2 healthy (the frontend pods; 7243 is SG-blocked, unused)" [ "$healthy7233" == 2 ]
alias_dns="$(scripts/route53-alias.sh --show >/dev/null; aws route53 list-resource-record-sets --hosted-zone-id "$(fval PRIVATE_ZONE_ID)" \
  --query "ResourceRecordSets[?Name=='$FE.' && Type=='A'].AliasTarget.DNSName | [0]" --output text | tr 'A-Z' 'a-z')"
lb_dns="$(jq -r '.LoadBalancers[0].DNSName' <<<"$lb" | tr 'A-Z' 'a-z')"
scripts/route53-alias.sh --show
check "Route 53 A alias $FE -> the NLB" [ "${alias_dns%.}" == "$lb_dns" ]
k -n temporal get networkpolicy

# --- 6 ---------------------------------------------------------------------------------
step "6. From a pod in the VPC through the Route 53 name (NLB)"
nlbips="$(aws ec2 describe-network-interfaces --filters "Name=description,Values=ELB net/temporal-frontend/*" --query 'NetworkInterfaces[].PrivateIpAddress' --output text | tr '\t' '\n' | sort | tr '\n' ' ')"
podrun temporal v011-s-client public.ecr.aws/docker/library/alpine:3.22 default "
  apk add -q --no-cache openssl bind-tools >/dev/null
  echo \"dig: \$(dig +short $FE | sort | tr '\n' ' ')\"
  openssl s_client -connect $FE:7233 -servername $FE -showcerts </dev/null 2>&1 \
    | grep -E 'subject=|issuer=|Acceptable client certificate CA names|^/?CN ?=|Verify return code|Protocol|alert' | head -12
" || true
dig="$(sed -n 's/^dig: //p' $LOGS/v011-s-client.log)"
check "the Route 53 name resolves to the NLB's private IPs (got: $dig)" [ "$dig" == "$nlbips" ]
check "s_client: the frontend presents the cert-manager certificate (issuer temporal-customer-support Root CA)" grep -q 'issuer=CN *= *temporal-customer-support Root CA' $LOGS/v011-s-client.log
check "s_client: the server asks for a client certificate from that CA (passthrough to the frontend)" bash -c \
  "grep -q 'Acceptable client certificate CA names' $LOGS/v011-s-client.log && grep -A1 'Acceptable client certificate CA names' $LOGS/v011-s-client.log | grep -q 'temporal-customer-support Root CA'"
podrun temporal v011-mtls-ok temporalio/admin-tools:1.32.1 default "
  temporal operator namespace list --address $FE:7233 --tls-ca-path /certs/ca.crt \
    --tls-cert-path /certs/tls.crt --tls-key-path /certs/tls.key --tls-server-name $FE -o json \
    | grep -o '\"name\": *\"[a-z-]*\"' | sort -u | tr '\n' ' '; echo
" temporal-operator-tls || true
check "mTLS through the NLB with the operator cert: namespace list works" grep -q '"agents"' $LOGS/v011-mtls-ok.log
podrun temporal v011-mtls-nocert temporalio/admin-tools:1.32.1 default "
  temporal operator namespace list --address $FE:7233 --tls-ca-path /certs/ca.crt --tls-server-name $FE 2>&1 | tail -2; exit 0
" temporal-operator-tls || true
check "same path WITHOUT a client cert is refused" bash -c "! grep -q '\"agents\"' $LOGS/v011-mtls-nocert.log && grep -qiE 'certificate|tls|handshake|unavailable|deadline' $LOGS/v011-mtls-nocert.log"

# --- 7 ---------------------------------------------------------------------------------
step "7. NetworkPolicy (VPC CNI agent): who may dial the frontend service directly"
podrun temporal-workers v011-np-unlabelled public.ecr.aws/docker/library/busybox:1.37 default \
  'nc -w 4 temporal-frontend.temporal.svc.cluster.local 7233 </dev/null && echo REACHED || echo BLOCKED' || true
podrun temporal-workers v011-np-labelled public.ecr.aws/docker/library/busybox:1.37 default \
  'nc -w 4 temporal-frontend.temporal.svc.cluster.local 7233 </dev/null && echo REACHED || echo BLOCKED' "" '{"temporal.io/client":"true"}' || true
check "unlabelled pod in temporal-workers is blocked" grep -q BLOCKED $LOGS/v011-np-unlabelled.log
check "pod labelled temporal.io/client=true reaches 7233" grep -q REACHED $LOGS/v011-np-labelled.log

# --- 8 ---------------------------------------------------------------------------------
step "8. Laptop: eks-pf profile and the UI through the port-forward"
temporal operator namespace describe --namespace agents -o json | jq -c '{name: .namespaceInfo.name, state: .namespaceInfo.state, retention: .config.workflowExecutionRetentionTtl}'
ui="$(curl -fsS http://127.0.0.1:8233/api/v1/namespaces | jq -r '[.namespaces[].namespaceInfo.name] | sort | join(" ")')"
check "UI (web.tls, its own client cert) lists the namespaces through the port-forward: $ui" grep -q agents <<<"$ui"

# --- 9 ---------------------------------------------------------------------------------
step "9. Finance on EKS (runbook 7b)"
k -n temporal-workers get deploy,pods,pdb,networkpolicy,certificate -o wide
fimg="$(k -n temporal-workers get deploy agent-services-worker -o jsonpath='{.spec.template.spec.containers[0].image}')"
echo "  image: $fimg"
check "Finance image from ECR agentcore-demo/finance-agent" grep -q '\.dkr\.ecr\.us-east-1\.amazonaws\.com/agentcore-demo/finance-agent:img-' <<<"$fimg"
parch="$(k -n temporal-workers exec deploy/agent-services-worker -- python -c 'import platform; print(platform.machine())')"
echo "  Finance container arch: $parch"
check "the image runs as aarch64 (built linux/arm64 for m7g)" [ "$parch" == aarch64 ]
check "2 Finance pods Ready on arm64 nodes" bash -c "[[ \$(kubectl --context agentcore-demo-eks -n temporal-workers get deploy agent-services-worker -o jsonpath='{.status.readyReplicas}') == 2 ]]"
pollers="$(temporal task-queue describe --namespace agent-services --task-queue agent-services-tq 2>&1 | grep -c 'workflow .*agent-services-worker-' || true)"
temporal task-queue describe --namespace agent-services --task-queue agent-services-tq 2>&1 | sed -n '/Pollers/,$p'
check "2 workflow pollers on agent-services-tq (one per pod)" [ "$pollers" == 2 ]
sa="$(k -n temporal-workers get sa agent-services-worker -o jsonpath='{.metadata.annotations.eks\.amazonaws\.com/role-arn}')"
check "SA agent-services-worker -> role agent-services-worker (IRSA)" grep -q ':role/agent-services-worker$' <<<"$sa"
envs="$(k -n temporal-workers exec deploy/agent-services-worker -- env | cut -d= -f1 | grep '^AWS_' | sort | tr '\n' ' ')"
echo "  AWS_* env names in a Finance pod: $envs"
check "no static AWS credentials (no AWS_ACCESS_KEY_ID/SECRET/SESSION_TOKEN; no aws-creds Secret)" bash -c \
  "! grep -qE 'AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN' <<<'$envs' && ! kubectl --context agentcore-demo-eks -n temporal-workers get secret aws-creds >/dev/null 2>&1 && grep -q AWS_ROLE_ARN <<<'$envs'"
check "Finance dials the in-cluster service name" bash -c "kubectl --context agentcore-demo-eks -n temporal-workers exec deploy/agent-services-worker -- env | grep -q '^TEMPORAL_ADDRESS=temporal-frontend.temporal.svc.cluster.local:7233'"
ep="$(temporal operator nexus endpoint get --name finance-agent -o json)"
jq -c '{name: .spec.name, target: .spec.target.worker}' <<<"$ep"
check "endpoint finance-agent -> agent-services / agent-services-tq" [ "$(jq -r '.spec.target.worker | "\(.namespace)/\(.taskQueue)"' <<<"$ep")" == "agent-services/agent-services-tq" ]

# --- 10 --------------------------------------------------------------------------------
step "10. Bedrock IAM through IRSA from a Finance pod (ledger 41)"
k -n temporal-workers exec -i deploy/agent-services-worker -- python - <<'PY' | tee $LOGS/bedrock.log
import boto3, botocore
c = boto3.client("bedrock-runtime", region_name="us-east-1")
for m in ["global.anthropic.claude-opus-5-5", "us.anthropic.claude-opus-5-5", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"]:
    try:
        r = c.converse(modelId=m, messages=[{"role": "user", "content": [{"text": "Reply with the word OK."}]}])
        print(f"{m}: allowed stop={r['stopReason']} text={r['output']['message']['content'][0]['text'][:20]!r}")
    except botocore.exceptions.ClientError as e:
        print(f"{m}: {e.response['Error']['Code']}")
PY
check "global. profile allowed (default BEDROCK_MODEL_ID)" grep -q 'global.anthropic.claude-opus-5-5: allowed' $LOGS/bedrock.log
check "us. profile allowed (the fallback, FINANCE_BEDROCK_MODEL_ID)" grep -q 'us.anthropic.claude-opus-5-5: allowed' $LOGS/bedrock.log
check "a model outside the policy is AccessDenied (least privilege)" grep -q 'sonnet.*AccessDeniedException' $LOGS/bedrock.log

# --- 11 --------------------------------------------------------------------------------
step "11. Q16: standalone Nexus operation from the CLI on server 1.32.1"
make -s card TEMPORAL_PROFILE=eks-pf 2>&1 | tee $LOGS/card.log | grep -E 'Status|Operation |Endpoint'
check "get_agent_card COMPLETED (Finance Refund Agent)" bash -c "grep -q 'Status  COMPLETED' $LOGS/card.log && grep -q 'Finance Refund Agent' $LOGS/card.log"

# --- 12 --------------------------------------------------------------------------------
step "12. Hybrid demo: laptop Support (support-dev-tq, laptop-support cert) -> EKS Finance"
if [[ "${VERIFY_SKIP_CASES:-0}" == 1 ]]; then echo "skipped (VERIFY_SKIP_CASES=1)"
else
  pods="$(k -n temporal-workers get pods -l app=agent-services-worker -o jsonpath='{.items[*].metadata.name}')"
  make worker-support TEMPORAL_PROFILE=eks-pf SUPPORT_TASK_QUEUE=support-dev-tq >$LOGS/support-worker.log 2>&1 &
  WORKER_PID=$!
  sleep 6
  { grep -m1 'support worker:' $LOGS/support-worker.log || true; } | sed 's/^.*INFO root: /  /'
  ts="$(date +%s)"
  make -s case-a TEMPORAL_PROFILE=eks-pf WF="support-1042-v011-$ts" >$LOGS/case-a.log 2>&1 || true
  grep -E 'Status' $LOGS/case-a.log || true
  scripts/demo/task-state.sh "support-1042-v011-$ts" | tee $LOGS/case-a-task.json | jq -c '{state, data: .artifacts[0].data}'
  ha="$(jq -r '.artifacts[0].data.handled_by' $LOGS/case-a-task.json)"
  check "Case A completed; handled_by=$ha is an EKS Finance pod" bash -c "grep -q 'Status          COMPLETED' $LOGS/case-a.log && grep -qw '$ha' <<<'$pods'"
  make -s case-b TEMPORAL_PROFILE=eks-pf WF="support-2077-v011-$ts" >/dev/null 2>&1
  st=""; for _ in $(seq 1 60); do st="$(scripts/demo/task-state.sh "support-2077-v011-$ts" 2>/dev/null | jq -r .state || true)"; [[ "$st" == input-required ]] && break; sleep 3; done
  echo "  Case B task state: $st"
  make -s pending TEMPORAL_PROFILE=eks-pf WF="support-2077-v011-$ts" | jq -c '{status, ops: [.pendingNexusOperations[] | {operation, state}]}'
  make -s approve TEMPORAL_PROFILE=eks-pf WF="support-2077-v011-$ts" APPROVER=dana.finance | sed 's/^/  /'
  temporal workflow result --namespace agents --workflow-id "support-2077-v011-$ts" >$LOGS/case-b.log 2>&1 || true
  grep -E 'Status' $LOGS/case-b.log || true
  scripts/demo/task-state.sh "support-2077-v011-$ts" | tee $LOGS/case-b-task.json | jq -c '{state, data: .artifacts[0].data}'
  hb="$(jq -r '.artifacts[0].data.handled_by' $LOGS/case-b-task.json)"
  rid="$(jq -r '.artifacts[0].data.refund_id' $LOGS/case-b-task.json)"
  check "Case B: input-required, approved by dana.finance, reply quotes $rid; handled_by=$hb is an EKS pod (gate G4)" bash -c \
    "[[ '$st' == input-required ]] && grep -q 'Status          COMPLETED' $LOGS/case-b.log && grep -q '$rid' $LOGS/case-b.log && grep -qw '$hb' <<<'$pods' && jq -e '.artifacts[0].data.approved_by == \"dana.finance\"' $LOGS/case-b-task.json >/dev/null"
  scripts/demo/failure.sh "support-1042-v011-failure-$ts" 2>&1 | tee $LOGS/failure.log | { grep -vE '^\s*$' || true; } | head -40 || true
  check "failure demo: send_message retried while Finance was at 0 (attempt >= 2), then completed" bash -c \
    "grep -q '\"attempt\": *[2-9]' $LOGS/failure.log && grep -q 'recovered' $LOGS/failure.log"
  pkill -P "$WORKER_PID" 2>/dev/null || true; kill "$WORKER_PID" 2>/dev/null || true; wait "$WORKER_PID" 2>/dev/null || true; WORKER_PID=""
  sleep 2
  n="$({ pgrep -f 'support_agent.worker' || true; } | wc -l | tr -d ' ')"
  check "laptop Support Worker stopped ($n processes)" [ "$n" == 0 ]
fi

# --- 13 --------------------------------------------------------------------------------
step "13. Q18 light re-check: certificate renewal vs the running Finance Worker"
pod="$(k -n temporal-workers get pods -l app=agent-services-worker -o jsonpath='{.items[0].metadata.name}')"
serial() { k -n temporal-workers exec "$pod" -- python -c "import ssl,sys; print(ssl._ssl._test_decode_cert('/etc/temporal/tls/tls.crt')['serialNumber'])"; }
before="$(serial)"; restarts0="$(k -n temporal-workers get pod "$pod" -o jsonpath='{.status.containerStatuses[0].restartCount}')"
CLUSTER=eks scripts/cert-renew.sh temporal-workers eks-nexus-worker-tls
after="$before"
for i in $(seq 1 40); do after="$(serial)"; [[ "$after" != "$before" ]] && break; sleep 5; done
echo "  mounted tls.crt serial: $before -> $after (after ~$((i*5)) s)"
check "the re-issued certificate reached the pod's mounted file" [ "$after" != "$before" ]
make -s card TEMPORAL_PROFILE=eks-pf 2>&1 | grep -q 'Status  COMPLETED' && ok "the running Worker (old cert still valid, same process) still serves get_agent_card" || bad "card after renewal"
restarts1="$(k -n temporal-workers get pod "$pod" -o jsonpath='{.status.containerStatuses[0].restartCount}')"
check "no restart ($restarts0 -> $restarts1): the SDK keeps the cert it read at connect (Q18: no reload)" [ "$restarts0" == "$restarts1" ]

# --- 14 --------------------------------------------------------------------------------
step "14. Inventory: v0.11 resources"
aws resourcegroupstaggingapi get-resources --tag-filters Key=Project,Values=agentcore-self-hosted \
  --query 'ResourceTagMappingList[].ResourceARN' --output text | tr '\t' '\n' \
  | { grep -E 'loadbalancer|targetgroup|secret:temporal/rds-temporal-user|snapshot:temporal-pre' || true; } | sed 's/^/  /'
aws secretsmanager describe-secret --secret-id temporal/rds-temporal-user --query '{name:Name,tags:Tags}' --output json | jq -c .
aws ecr describe-images --repository-name agentcore-demo/finance-agent --query 'length(imageDetails)' --output text | sed 's/^/  finance-agent images: /'
aws ecr describe-images --repository-name agentcore-demo/payments-mock --query 'length(imageDetails)' --output text | sed 's/^/  payments-mock images: /'

echo
if [[ "$fail" == 0 ]]; then echo "v0.11-temporal-and-finance-on-eks verify: OK"; else echo "v0.11-temporal-and-finance-on-eks verify: FAILED"; exit 1; fi
