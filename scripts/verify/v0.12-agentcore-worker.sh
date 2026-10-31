#!/usr/bin/env bash
# Observable acceptance checks for v0.12-agentcore-worker (IMPLEMENTATION_PLAN.md v0.12;
# runbook §5 "IAM", "AgentCore and the Worker", "End to end" first half; Phase 8 steps 1-5).
# us-east-1, default SSO profile. Output goes through scripts/redact.sh.
#
# It creates and deletes NO AWS resources. The make targets did that, in this order:
#   make export-agentcore-cert CLUSTER=eks CONFIRM=yes      secret temporal/agentcore-worker-mtls
#   make tf-apply STACK=30-agentcore-iam                    (create_dev_wci_role = false)
#   make agentcore-deploy MODE=VPC                          runtime + endpoint b_<build7>
#   make invoke-role                                        CFN stack trusting temporal-server-wci
#   make register-version MODE=--agentcore-auto CLOUD=1 TEMPORAL_PROFILE=eks-pf
# Temporal side it starts Workflows on support-tq (ping, Case A: real Opus 5.5 inside AgentCore),
# runs one short-lived aws-cli pod (deleted again), and starts/stops the port-forward.
#   1  make check (VERIFY_SKIP_CHECK=1 skips), make tf-static
#   2  Build ID = build-id.sh at HEAD, HEAD on main
#   3  IAM: execution role (secret + Bedrock statements), temporal-dev-wci gone, invocation role
#      stack (trusts temporal-server-wci, resource = runtime ARN + *, External ID enforced:
#      denied without it, also from the temporal-worker IRSA identity)
#   4  mTLS secret: keys, CN agentcore-worker from the EKS CA, ~180 d, tags; kind secret absent
#   5  runtime: VPC mode (subnets on supported AZ IDs, sg-agentcore-worker), AWS_IAM, execution
#      role, idle 120 s, endpoint b_<build7> READY, env deployment/Build ID/TLS/secret wired
#   6  registration: current version = Build ID, compute config aws-agentcore
#   7  zero pollers on support-tq (workflow + activity), versioned backlog 0
#   8  ping (PingWorkflow on support-tq), then Case A, nothing else polling (VERIFY_SKIP_CASES=1 skips)
#   9  WCI history (InvokeWorker), CloudTrail (AssumeRole with External ID by temporal-server-wci,
#      GetAgentRuntimeEndpoint), Invocations metric (InvokeAgentRuntime is a data event)
#   10 AgentCore logs: mTLS from Secrets Manager, connected to the Route 53 name, polling, idle 30 s,
#      drained, async task released
#   11 NLB cross-zone on; VPC flow logs: AgentCore ENI -> NLB:7233 ACCEPT; every destination (AWS Q2)
#   12 sessions back to 0 (no new session of ours in the last 5 min), pollers gone, no local processes
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
exec > >(scripts/redact.sh) 2>&1
export AWS_REGION=us-east-1 AWS_PAGER=""
unset AWS_PROFILE
export KUBECONFIG="$PWD/.temporal/kubeconfig-eks"
export TEMPORAL_CONFIG_FILE="$PWD/config/temporal.toml" TEMPORAL_PROFILE=eks-pf
fval() { (set +u; . .generated/foundation.env; eval "printf '%s' \"\${$1}\""); }
ZONE_NAME="$(fval PRIVATE_ZONE_NAME)"
FE="temporal-frontend.$ZONE_NAME"
LOGS=.temporal/verify-v0.12
rm -rf "$LOGS"; mkdir -p "$LOGS"
fail=0
step() { printf '\n=== %s  (%s)\n' "$*" "$(date -u +%H:%M:%SZ)"; }
ok()  { printf '  ok    %s\n' "$*"; }
bad() { printf '  FAIL  %s\n' "$*"; fail=1; }
check() { local what="$1"; shift; if "$@"; then ok "$what"; else bad "$what"; fi; }
k() { kubectl --context agentcore-demo-eks "$@"; }
PF_STARTED=0
cleanup() {
  k -n temporal delete pod v012-awscli --ignore-not-found --wait=false >/dev/null 2>&1 || true
  if [[ "$PF_STARTED" == 1 ]]; then scripts/eks.sh pf-stop >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT
NS=agents DEP=support-supervisor TQ=support-tq
RUNTIME_FULL=AgentcoreDemo_default_support_supervisor

# --- 1 ---------------------------------------------------------------------------------
step "1. make check, make tf-static"
if [[ "${VERIFY_SKIP_CHECK:-0}" == 1 ]]; then echo "  skipped (VERIFY_SKIP_CHECK=1)"
else
  check "make check" bash -c "make check >$LOGS/check.log 2>&1"
fi
make -s tf-static 2>&1 | grep -E 'valid|fmt ok|tflint ok' | sort | uniq -c

# --- 2 ---------------------------------------------------------------------------------
step "2. Build ID (content hash at HEAD) and the main-only registration rule"
BUILD="$(scripts/build-id.sh)"; EP="b_${BUILD:0:7}"
echo "  Build ID $BUILD, endpoint $EP, HEAD $(git rev-parse --short HEAD) on $(git rev-parse --abbrev-ref HEAD)"
check "HEAD is on main (plan 3.3: cloud versions from main only)" git merge-base --is-ancestor HEAD main

# --- 3 ---------------------------------------------------------------------------------
step "3. IAM: execution role, no temporal-dev-wci, invocation role (self-hosted template, External ID)"
pol="$(aws iam get-role-policy --role-name agentcore-demo-support-exec --policy-name agentcore-demo-support-exec --query PolicyDocument --output json)"
jq -r '.Statement[] | select(.Sid=="MtlsSecret" or .Sid=="BedrockPinnedModel") | "  \(.Sid): \(.Action | if type=="array" then join(",") else . end) on \([.Resource] | flatten | join(" "))"' <<<"$pol"
check "execution role reads only temporal/agentcore-worker-mtls-* and invokes only the Opus 5.5 profiles/model" bash -c \
  "jq -e '[.Statement[] | select(.Sid==\"MtlsSecret\") | .Resource] | flatten | all(test(\":secret:temporal/agentcore-worker-mtls-\\\\*\$\"))' <<<'$pol' >/dev/null && jq -e '[.Statement[] | select(.Sid==\"BedrockPinnedModel\") | .Resource] | flatten | all(test(\"claude-opus-5-5\$\"))' <<<'$pol' >/dev/null"
check "role temporal-dev-wci is gone (v0.8 only; create_dev_wci_role = false)" bash -c "! aws iam get-role --role-name temporal-dev-wci >/dev/null 2>&1"
scripts/invoke-role.sh status | tee $LOGS/invoke-role.json | sed 's/^/  /'
trust="$(aws iam get-role --role-name Temporal-AgentCore-Worker --query Role.AssumeRolePolicyDocument --output json)"
jq -c '.Statement[] | {principal: .Principal, condition: (.Condition | keys)}' <<<"$trust" | sed 's/^/  trust /'
check "invocation role trusts the temporal-server-wci ROLE ARN and requires sts:ExternalId" bash -c \
  "jq -e '[.Statement[] | (.Principal.AWS | tostring | test(\":role/temporal-server-wci\$\")) and (.Condition.StringEquals[\"sts:ExternalId\"] != null)] | any' <<<'$trust' >/dev/null"
check "invocation role resource = <runtime ARN>* (endpoints included)" grep -qE 'runtime/[^"/]+\*"' $LOGS/invoke-role.json
if aws sts assume-role --role-arn "$(scripts/invoke-role.sh arn)" --role-session-name v012-noextid >/dev/null 2>&1; then
  bad "AssumeRole as the SSO admin without External ID was allowed"
else ok "AssumeRole as the SSO admin (not trusted, no External ID): denied"; fi
# From the WCI's own identity (IRSA on temporal-worker), WITHOUT the External ID: must be denied.
# (With it, the WCI history and CloudTrail below show it allowed; the ID never goes into a pod spec.)
role_arn="$(scripts/invoke-role.sh arn)"
k -n temporal delete pod v012-awscli --ignore-not-found >/dev/null 2>&1
jq -n --arg r "$role_arn" '{apiVersion:"v1",kind:"Pod",metadata:{name:"v012-awscli",namespace:"temporal"},
  spec:{restartPolicy:"Never",serviceAccountName:"temporal-worker",containers:[{name:"c",image:"amazon/aws-cli:2.27.50",
  command:["sh","-c",("aws sts get-caller-identity --query Arn --output text; if aws sts assume-role --role-arn " + $r + " --role-session-name v012-irsa-noextid >/dev/null 2>/tmp/e; then echo ALLOWED; else echo DENIED; grep -o \"AccessDenied\" /tmp/e | head -1; fi")]}]}}' \
  | k apply -f - >/dev/null
for _ in $(seq 1 60); do ph="$(k -n temporal get pod v012-awscli -o jsonpath='{.status.phase}' 2>/dev/null || true)"; [[ "$ph" == Succeeded || "$ph" == Failed ]] && break; sleep 2; done
k -n temporal logs v012-awscli 2>&1 | tee $LOGS/irsa-noextid.log | sed 's/^/    /'
k -n temporal delete pod v012-awscli --ignore-not-found --wait=false >/dev/null 2>&1
check "AssumeRole as temporal-server-wci (IRSA) WITHOUT the External ID: denied" bash -c \
  "grep -q 'assumed-role/temporal-server-wci/' $LOGS/irsa-noextid.log && grep -q DENIED $LOGS/irsa-noextid.log"

# --- 4 ---------------------------------------------------------------------------------
step "4. mTLS secret temporal/agentcore-worker-mtls (Q5 production form)"
scripts/export-agentcore-cert.sh --cluster eks --secret-id temporal/agentcore-worker-mtls --check | tee $LOGS/secret.log
check "secret has tls_crt,tls_key,ca_crt; CN agentcore-worker issued by the EKS root CA; tag Project" bash -c \
  "grep -q 'keys: ca_crt,tls_crt,tls_key' $LOGS/secret.log && grep -q 'CN *= *agentcore-worker' $LOGS/secret.log && grep -q 'agentcore-self-hosted' $LOGS/secret.log"
nb="$(grep notBefore $LOGS/secret.log | cut -d= -f2-)"; na="$(grep notAfter $LOGS/secret.log | cut -d= -f2-)"
days=$(( ( $(date -j -f '%b %e %T %Y %Z' "$na" +%s) - $(date -j -f '%b %e %T %Y %Z' "$nb" +%s) ) / 86400 ))
check "certificate lifetime ${days} d (4320h = 180 d)" [ "$days" -eq 180 ]
check "no local copy of the AgentCore identity (.certs/eks/agentcore-worker shredded)" bash -c "[[ ! -e .certs/eks/agentcore-worker ]]"
check "temporal-kind/agentcore-worker-mtls does not exist (never created at v0.9)" bash -c \
  "! aws secretsmanager describe-secret --secret-id temporal-kind/agentcore-worker-mtls >/dev/null 2>&1"

# --- 5 ---------------------------------------------------------------------------------
step "5. AgentCore runtime in VPC mode (AgentCore CLI; ledger 42)"
scripts/agentcore-deploy.sh network | tee $LOGS/network.json
rid="$(aws bedrock-agentcore-control list-agent-runtimes --query "agentRuntimes[?agentRuntimeName=='${RUNTIME_FULL}'].agentRuntimeId | [0]" --output text)"
rt="$(aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$rid" --output json)"
subnets="$(jq -r '.networkConfiguration.networkModeConfig.subnets[]' <<<"$rt")"
azids="$(aws ec2 describe-subnets --subnet-ids $subnets --query 'Subnets[].AvailabilityZoneId' --output text | tr '\t' ' ')"
echo "  subnet AZ IDs: $azids (AgentCore us-east-1: use1-az1 use1-az2 use1-az4)"
want_sg="$(fval AGENTCORE_SECURITY_GROUPS)"; want_sn="$(fval AGENTCORE_SUBNETS | tr ',' '\n' | sort | tr '\n' ' ')"
check "networkMode VPC, the three private subnets, only sg-agentcore-worker" bash -c \
  "jq -e '.networkConfiguration.networkMode==\"VPC\" and .networkConfiguration.networkModeConfig.securityGroups==[\"$want_sg\"]' <<<'$rt' >/dev/null && [[ \"\$(jq -r '.networkConfiguration.networkModeConfig.subnets[]' <<<'$rt' | sort | tr '\n' ' ')\" == '$want_sn' ]]"
check "subnets only on supported AZ IDs" bash -c "for a in $azids; do [[ \$a == use1-az1 || \$a == use1-az2 || \$a == use1-az4 ]] || exit 1; done"
check "execution role = agentcore-demo-support-exec (existing role, not a CLI-created one)" bash -c "jq -e '.roleArn | test(\":role/agentcore-demo-support-exec\$\")' <<<'$rt' >/dev/null"
check "idle session timeout 120 s, maxLifetime 28800 s" bash -c "jq -e '.lifecycleConfiguration.idleRuntimeSessionTimeout==120 and .lifecycleConfiguration.maxLifetime==28800' <<<'$rt' >/dev/null"
check "authorizer AWS_IAM (no JWT authorizer configured)" bash -c "jq -e '.authorizerConfiguration == null' <<<'$rt' >/dev/null"
env_json="$(jq -c '.environmentVariables // {}' <<<"$rt")"
jq -r 'to_entries[] | select(.key|test("TEMPORAL_(ADDRESS|NAMESPACE|TASK_QUEUE|DEPLOYMENT_NAME|BUILD_ID|TLS|TLS_SERVER_NAME|MTLS_SECRET_ID)$|AGENTCORE_DEBOUNCE|BEDROCK_MODEL")) | "  env \(.key)=\(.value)"' <<<"$env_json"
check "env: address = Route 53 name:7233, TLS on, server name, secret ID, deployment, Build ID $BUILD, debounce 30" bash -c \
  "jq -e --arg fe '$FE' --arg b '$BUILD' '.TEMPORAL_ADDRESS==(\$fe+\":7233\") and .TEMPORAL_TLS==\"true\" and .TEMPORAL_TLS_SERVER_NAME==\$fe and .TEMPORAL_MTLS_SECRET_ID==\"temporal/agentcore-worker-mtls\" and .TEMPORAL_DEPLOYMENT_NAME==\"support-supervisor\" and .TEMPORAL_BUILD_ID==\$b and .TEMPORAL_TASK_QUEUE==\"support-tq\" and .AGENTCORE_DEBOUNCE_SECONDS==\"30\"' <<<'$env_json' >/dev/null"
check "named endpoint $EP READY (not DEFAULT); name matches [a-zA-Z][a-zA-Z0-9_]{0,47}" bash -c \
  "aws bedrock-agentcore-control list-agent-runtime-endpoints --agent-runtime-id '$rid' --query \"runtimeEndpoints[?name=='$EP'].status | [0]\" --output text | grep -qx READY"
echo "  service-linked role: $(aws iam get-role --role-name AWSServiceRoleForBedrockAgentCoreNetwork --query 'Role.RoleName' --output text 2>/dev/null || echo absent)"

# --- 6 ---------------------------------------------------------------------------------
if ! nc -z 127.0.0.1 7233 2>/dev/null; then scripts/eks.sh pf >/dev/null; PF_STARTED=1; fi
step "6. Registration on EKS (create -> create-version -> set-current-version)"
temporal worker deployment describe --namespace $NS --name $DEP -o json >$LOGS/deployment.json
# CLI 1.9.1 JSON: routingConfig.currentVersionBuildID, versionSummaries[].BuildID; the
# version's computeConfig is top level.
jq -c '{current: .routingConfig.currentVersionBuildID, versions: [.versionSummaries[]? | .BuildID]}' $LOGS/deployment.json | sed 's/^/  /'
temporal worker deployment describe-version --namespace $NS --deployment-name $DEP --build-id "$BUILD" -o json >$LOGS/version.json
jq -c '{currentSince: .currentSinceTime, provider: .computeConfig.scalingGroups.default.provider.type, scaler: .computeConfig.scalingGroups.default.scaler.type}' $LOGS/version.json | sed 's/^/  /'
check "current version is $DEP:$BUILD" bash -c "jq -e --arg b '$BUILD' '.routingConfig.currentVersionBuildID==\$b' $LOGS/deployment.json >/dev/null"
check "compute config: aws-agentcore" bash -c "jq -e '.computeConfig.scalingGroups.default.provider.type==\"aws-agentcore\"' $LOGS/version.json >/dev/null"

pollers() {  # count of pollers on support-tq (workflow + activity task queues)
  temporal task-queue describe --namespace $NS --task-queue $TQ -o json 2>/dev/null \
    | jq '[.. | objects | select(has("pollers")) | .pollers[]?] | length' 2>/dev/null || echo 0
}
# --- 7 ---------------------------------------------------------------------------------
step "7. Nothing polls support-tq before the probe (sessions from the registration drained)"
n=0; for _ in $(seq 1 40); do n="$(pollers)"; [[ "$n" == 0 ]] && break; sleep 15; done
temporal task-queue describe --namespace $NS --task-queue $TQ 2>&1 | head -20 | sed 's/^/  /'
check "zero pollers on support-tq" [ "$n" == 0 ]

# --- 8 ---------------------------------------------------------------------------------
ts="$(date +%s)"; T0="$(date -u +%FT%T.000Z)"; T0ms=$(( $(date +%s) * 1000 ))
if [[ "${VERIFY_SKIP_CASES:-0}" == 1 ]]; then step "8. skipped (VERIFY_SKIP_CASES=1)"
else
  step "8. ping, then Case A, with nothing but AgentCore able to poll support-tq"
  PING="${VERIFY_PING_ID:-ping-1}"
  temporal workflow describe --namespace $NS --workflow-id "$PING" >/dev/null 2>&1 && PING="ping-v012-$ts"
  s=$(date +%s)
  temporal workflow execute --namespace $NS --task-queue $TQ --type PingWorkflow --workflow-id "$PING" --execution-timeout 15m \
    --input "\"$PING\"" >$LOGS/ping.log 2>&1 || true
  grep -E 'Status|Result' $LOGS/ping.log | sed 's/^/  /'
  check "$PING completed in $(( $(date +%s) - s )) s (cold start included) on an AgentCore Worker of $BUILD" bash -c \
    "grep -q 'Status          COMPLETED' $LOGS/ping.log && grep -q '$BUILD' $LOGS/ping.log"
  pods="$(k -n temporal-workers get pods -l app=agent-services-worker -o jsonpath='{.items[*].metadata.name}')"
  WF="support-1042-v012-$ts"; s=$(date +%s)
  SUPPORT_TASK_QUEUE=$TQ scripts/demo/case-a.sh "$WF" >$LOGS/case-a.log 2>&1 || true
  grep -E 'Status' $LOGS/case-a.log | sed 's/^/  /'
  scripts/demo/task-state.sh "$WF" | tee $LOGS/case-a-task.json | jq -c '{state, data: .artifacts[0].data}'
  ha="$(jq -r '.artifacts[0].data.handled_by' $LOGS/case-a-task.json)"
  check "Case A ($WF) completed in $(( $(date +%s) - s )) s; refund handled_by=$ha is an EKS Finance pod" bash -c \
    "grep -q 'Status          COMPLETED' $LOGS/case-a.log && grep -qw '$ha' <<<'$pods'"
  temporal workflow show --namespace $NS --workflow-id "$WF" -o json >$LOGS/case-a-history.json
  jq -r '[.events[] | select(.eventType=="EVENT_TYPE_NEXUS_OPERATION_SCHEDULED") | .nexusOperationScheduledEventAttributes.operation] | "  Nexus operations: " + join(", ")' $LOGS/case-a-history.json
  jq -r '[.events[] | select(.eventType=="EVENT_TYPE_WORKFLOW_TASK_COMPLETED") | .workflowTaskCompletedEventAttributes.identity] | unique | "  Workflow Task identities: " + join(", ")' $LOGS/case-a-history.json
  check "every Workflow Task of Case A ran on an AgentCore lifetime (identity .../agentcore-lifetime-N)" bash -c \
    "jq -e '[.events[] | select(.eventType==\"EVENT_TYPE_WORKFLOW_TASK_COMPLETED\") | .workflowTaskCompletedEventAttributes.identity] | all(test(\"agentcore-lifetime-\"))' $LOGS/case-a-history.json >/dev/null"
  check "Case A history has get_agent_card and send_message Nexus events" bash -c \
    "jq -e '[.events[] | select(.eventType==\"EVENT_TYPE_NEXUS_OPERATION_SCHEDULED\") | .nexusOperationScheduledEventAttributes.operation] | (index(\"get_agent_card\") != null and index(\"send_message\") != null)' $LOGS/case-a-history.json >/dev/null"
  jq -r '.workflowExecutionInfo // empty' <(temporal workflow describe --namespace $NS --workflow-id "$WF" -o json) >/dev/null
  temporal workflow describe --namespace $NS --workflow-id "$WF" -o json \
    | jq -c '{versioning: .workflowExecutionInfo.versioningInfo | {behavior, version: (.deploymentVersion.buildId // .version)}}' | sed 's/^/  /'
fi

# --- 9 ---------------------------------------------------------------------------------
step "9. WCI history, CloudTrail (AssumeRole with External ID, GetAgentRuntimeEndpoint), invocations"
scripts/wci-history.sh "$BUILD" --since "$T0" | tee $LOGS/wci.log | tail -25
inv="$(grep -c 'activity  InvokeWorker$' $LOGS/wci.log || true)"
check "WCI invoked AgentCore during the probe ($inv InvokeWorker)" [ "$inv" -ge 1 ]
check "no failed WCI activity during the probe" bash -c "! grep -q '  failed  ' $LOGS/wci.log"
sleep "${VERIFY_TRAIL_WAIT:-0}"
aws cloudtrail lookup-events --start-time "$(date -u -v-3H +%FT%TZ)" \
  --lookup-attributes AttributeKey=EventName,AttributeValue=AssumeRole --max-results 50 --output json \
  | jq -r '.Events[] | .CloudTrailEvent | fromjson | select(.requestParameters.roleArn // "" | test(":role/Temporal-AgentCore-Worker$"))
      | "\(.eventTime)  AssumeRole by \(.userIdentity.sessionContext.sessionIssuer.userName // .userIdentity.type)  externalId=\(if .requestParameters.externalId then "present" else "absent" end)  \(.errorCode // "ok")  \(.userAgent | split(" ")[0])"' \
  | sort | tee $LOGS/trail-assume.log | tail -8 | sed 's/^/  /'
check "CloudTrail: AssumeRole of Temporal-AgentCore-Worker by temporal-server-wci WITH the External ID, ok" \
  grep -q 'AssumeRole by temporal-server-wci  externalId=present  ok' $LOGS/trail-assume.log
aws cloudtrail lookup-events --start-time "$(date -u -v-3H +%FT%TZ)" \
  --lookup-attributes AttributeKey=EventName,AttributeValue=GetAgentRuntimeEndpoint --max-results 20 --output json \
  | jq -r '.Events[] | .CloudTrailEvent | fromjson | "\(.eventTime)  GetAgentRuntimeEndpoint  \(.userIdentity.sessionContext.sessionIssuer.userName // .userIdentity.type)  \(.errorCode // "ok")  \(.userAgent | split(" ")[0])"' \
  | sort | tee $LOGS/trail-get.log | tail -4 | sed 's/^/  /'
check "CloudTrail: GetAgentRuntimeEndpoint as Temporal-AgentCore-Worker, ok" grep -q 'GetAgentRuntimeEndpoint  Temporal-AgentCore-Worker  ok' $LOGS/trail-get.log

# --- 10 --------------------------------------------------------------------------------
step "10. AgentCore logs of $EP: mTLS from Secrets Manager -> Route 53 name -> poll -> work -> drain"
# Only the probe's lifetime counts (lines at or after the probe start). Its drain comes 30 s
# after Case A's last Activity, so wait for "async task released" (up to ~4 min).
P0="$(date -u -r "$ts" '+%F %T')"
for _ in $(seq 1 16); do
  since_min=$(( ( $(date +%s) - ts ) / 60 + 3 ))
  scripts/agentcore-deploy.sh logs "$since_min" >$LOGS/agentcore.log 2>&1 || true
  awk -v p0="$P0" '/^20[0-9][0-9]-[0-9][0-9]-[0-9][0-9] / { if (($1 " " substr($2,1,8)) >= p0) print }' \
    $LOGS/agentcore.log >$LOGS/agentcore-probe.log
  grep -q 'async task released' $LOGS/agentcore-probe.log && break
  sleep 15
done
PL=$LOGS/agentcore-probe.log
grep -E 'AgentCore app:|mTLS:|connected to|polling|idle for|drained|async task released|failed|ERROR|refusing' $PL | tail -30 | sed 's/^/  /' || true
check "mTLS material loaded from Secrets Manager" grep -q 'mTLS: client cert, key and CA loaded from Secrets Manager' $PL
check "connected to $FE:7233 with TLS, server name = the Route 53 name, client certificate yes" \
  grep -q "connected to $FE:7233 namespace agents (TLS, server name $FE, client certificate yes)" $PL
check "polled support-tq as $DEP:$BUILD (PINNED), debounce 30 s" grep -q "polling agents/support-tq as $DEP:$BUILD (PINNED), debounce 30s" $PL
check "idle for 30 s -> drained -> async task released (complete_async_task in finally)" bash -c \
  "grep -q 'idle for 30s' $PL && grep -q ': drained' $PL && grep -q 'async task released' $PL"
check "no failed lifetime, no refused invocation" bash -c "! grep -qE 'worker lifetime [0-9]+ failed|refusing to start' $PL"

# --- 11 --------------------------------------------------------------------------------
step "11. VPC flow logs: the AgentCore ENIs (AWS Q2)"
# Found at v0.12: 3 NLB nodes, frontend pods in 2 AZs. With cross-zone off a session whose
# ENI resolved the third AZ's NLB IP timed out connecting (values.eks.yaml, ledger 70).
lb="$(aws elbv2 describe-load-balancers --names temporal-frontend --query 'LoadBalancers[0].LoadBalancerArn' --output text)"
check "NLB temporal-frontend: cross-zone load balancing enabled" bash -c \
  "aws elbv2 describe-load-balancer-attributes --load-balancer-arn '$lb' --query 'Attributes[?Key==\`load_balancing.cross_zone.enabled\`].Value' --output text | grep -qx true"
for i in $(seq 1 "${VERIFY_FLOW_TRIES:-20}"); do
  $(command -v uv) run --frozen python scripts/flow_report.py --since $(( ( $(date +%s) - ts ) / 60 + 10 )) >$LOGS/flow.log 2>&1 || true
  grep -q 'NLB temporal-frontend *7233 ACCEPT' $LOGS/flow.log && break
  sleep 30
done
sed 's/^/  /' $LOGS/flow.log
check "flow logs: AgentCore ENI -> NLB temporal-frontend:7233 ACCEPT" grep -q 'NLB temporal-frontend *7233 ACCEPT' $LOGS/flow.log
check "nothing from the AgentCore ENIs left the VPC (no NAT path used)" bash -c "! grep -q 'outside the VPC .*ACCEPT' $LOGS/flow.log"

# --- 12 --------------------------------------------------------------------------------
step "12. Back to zero: sessions, pollers, local processes"
last="$(grep -E 'async task released' $LOGS/agentcore.log | tail -1 | cut -c1-19 || true)"
echo "  last 'async task released': ${last:-?} UTC; waiting for 5 quiet minutes"
n=1; for _ in $(seq 1 "${VERIFY_SESSION_TRIES:-16}"); do
  if scripts/agentcore-sessions.sh --window 5 >$LOGS/sessions.log 2>&1; then n=0; break; fi
  sleep 60
done
sed 's/^/  /' $LOGS/sessions.log
check "no new session of ours in the last 5 min (idle 120 s: every session has ended)" [ "$n" == 0 ]
check "zero pollers on support-tq again" [ "$(pollers)" == 0 ]
check "no laptop Worker running" bash -c "! pgrep -f 'support_agent\.(worker|agentcore_app)' >/dev/null"

echo
if [[ $fail -eq 0 ]]; then echo "verify: OK"; else echo "verify: FAILED"; exit 1; fi
