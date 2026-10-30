#!/usr/bin/env bash
# Observable acceptance checks for v0.8-wci-handshake (IMPLEMENTATION_PLAN.md v0.8).
# First AWS contact: us-east-1 only, default SSO profile, go-ahead G5. Output goes through
# scripts/redact.sh (account IDs, ARNs, emails, IPs, External-ID-shaped hex are masked).
#
# Part 1 (steps 1-9) is static or read-only AWS: tools, Terraform static checks, the vendored
# CFN template, the guardrails, the External ID secret (presence only), the provider schema
# (AWS Q4/Q6), the AgentCore CLI's synth of the PUBLIC probe (CodeZip, existing role), the
# session check. Then a GATE: the 30-agentcore-iam roles must exist (exit 3 "BLOCKED" if not).
# Part 2 (steps 10-17) creates and then DELETES the throwaway resources the same day:
# PUBLIC runtime (AgentCore CLI stack), CFN invocation-role stack; it runs the dev server with
# the WCI as temporal-dev-wci, registration with a local versioned Worker, the wrong-External-ID
# negative test, the ping-1 invocation probe, the Q15 probe, and the teardown (step 17).
#   VERIFY_SKIP_CHECK=1       skip step 1 (make check)
#   VERIFY_PART=1             stop after the gate (no resources created)
#   VERIFY_WATCH_SECONDS=300  how long steps 15/16 watch the WCI (plan: 5 min)
#   VERIFY_KEEP=1             skip the teardown (NOT for normal runs: same-day teardown is the rule)
# Needs a committed tree (scripts/build-id.sh) and `make tools tools-agentcore`.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
exec > >(scripts/redact.sh) 2>&1

export AWS_REGION=us-east-1 TEMPORAL_CONFIG_FILE="$PWD/config/temporal.toml" TEMPORAL_PROFILE=local
export PAYMENTS_URL=http://127.0.0.1:8081
unset TEMPORAL_TASK_QUEUE TEMPORAL_BUILD_ID AWS_PROFILE
TF=.tools/bin/terraform
UV=(uv run --frozen)
LOGS=.temporal/verify-v0.8
rm -rf "$LOGS"; mkdir -p "$LOGS"
NS=agents DEP=support-supervisor TQ=support-tq
WATCH="${VERIFY_WATCH_SECONDS:-300}"
step() { printf '\n=== %s  (%s)\n' "$*" "$(date -u +%H:%M:%SZ)"; }
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
# macOS has no timeout(1): run "$@", kill it after $1 seconds.
with_timeout() {
  local secs="$1"; shift
  "$@" & local pid=$!
  ( sleep "$secs"; kill "$pid" 2>/dev/null ) & local killer=$!
  local rc=0; wait "$pid" || rc=$?
  kill "$killer" 2>/dev/null || true; wait "$killer" 2>/dev/null || true
  return $rc
}
pids=()
cleanup() {
  for p in ${pids[@]+"${pids[@]}"}; do kill "$p" 2>/dev/null || true; done
  for p in ${pids[@]+"${pids[@]}"}; do wait "$p" 2>/dev/null || true; done
  scripts/dev-server.sh stop >/dev/null 2>&1 || true
}
trap cleanup EXIT

step "1. make check"
if [[ "${VERIFY_SKIP_CHECK:-0}" == 1 ]]; then echo "SKIPPED (VERIFY_SKIP_CHECK=1)"; else make -s check | tail -1; fi

step "2. tools (project-local, pinned sha256 / exact npm version)"
$TF version | sed -n 1p
.tools/bin/tflint --version | sed -n 1p
echo "agentcore $(scripts/agentcore.sh --version | tail -1)"
echo "temporal  $(temporal --version)"
echo "aws       $(aws --version 2>&1 | cut -d' ' -f1)"

step "3. Terraform static (fmt -check, validate -backend=false, tflint)"
scripts/tf.sh static

step "4. Vendored invocation-role template (runbook 6.2)"
scripts/cfn-template.sh --check
T=deploy/agentcore/temporal-self-hosted-serverless-worker-agentcore-role.yaml
echo "parameters: $(sed -n '/^Parameters:/,/^Resources:/p' "$T" | grep -E '^  [A-Za-z]+:' | tr -d ' :' | paste -sd, -)"
echo "outputs:    $(sed -n '/^Outputs:/,$p' "$T" | grep -E '^  [A-Za-z]+:' | tr -d ' :' | paste -sd, -)"
echo "trust:      $(grep -A1 "Condition:" "$T" | tail -1 | tr -s ' ') sts:ExternalId; MaxSessionDuration $(awk '/MaxSessionDuration/{print $2}' "$T")"

step "5. Guardrails (00-guardrails): budget, AgentCore session alarm"
acct="$(aws sts get-caller-identity --query Account --output text)"
aws budgets describe-budget --account-id "$acct" --budget-name agentcore-demo-monthly \
  --query 'Budget.{name: BudgetName, limit: BudgetLimit, unit: TimeUnit}' --output json
aws budgets describe-notifications-for-budget --account-id "$acct" --budget-name agentcore-demo-monthly \
  --query 'Notifications[].[NotificationType, Threshold, ThresholdType]' --output text | sort
echo "subscribers per notification (addresses not shown): $(aws budgets describe-subscribers-for-notification \
  --account-id "$acct" --budget-name agentcore-demo-monthly \
  --notification NotificationType=ACTUAL,ComparisonOperator=GREATER_THAN,Threshold=100,ThresholdType=PERCENTAGE \
  --query 'length(Subscribers)' --output text)"
aws cloudwatch describe-alarms --alarm-names agentcore-demo-active-sessions \
  --query 'MetricAlarms[0].{alarm: AlarmName, ns: Namespace, metric: MetricName, dims: Dimensions, threshold: Threshold, periods: EvaluationPeriods, state: StateValue}' --output json
tf00="$($TF -chdir=infra/terraform/00-guardrails state list | paste -sd' ' -)"
echo "00-guardrails state: $tf00"

step "6. External ID secret (presence and length only; runbook 6.1, ADR-0007)"
scripts/external-id.sh --check
echo "tags: $(aws secretsmanager describe-secret --secret-id temporal/agentcore-external-id --query 'Tags[?Key==`Project`].Value' --output text)"
if grep -rqs AssumeRoleExternalId infra/terraform/*/terraform.tfstate; then echo "FAIL: External ID in Terraform state"; exit 1; fi
echo "not in any Terraform state file: ok"

step "7. AWS Q6 / Q4: AgentCore in the Terraform AWS provider schema (informational; ADR-0008 keeps the CLI)"
schema="$LOGS/aws-schema.json"
scripts/tf.sh init 30-agentcore-iam   # local backend; creates no state
$TF -chdir=infra/terraform/30-agentcore-iam providers schema -json >"$schema"
echo "provider: $(jq -r '.provider_schemas | keys[0]' "$schema") $(grep -A1 'hashicorp/aws' infra/terraform/30-agentcore-iam/.terraform.lock.hcl | awk -F'"' '/version/{print $2}')"
jq -r '.provider_schemas[].resource_schemas | keys[] | select(startswith("aws_bedrockagentcore_"))' "$schema" | paste -sd' ' - | fold -s -w 110 | sed 's/^/  /'
echo "aws_bedrockagentcore_agent_runtime.environment_variables: $(jq -c '.provider_schemas[].resource_schemas.aws_bedrockagentcore_agent_runtime.block.attributes.environment_variables.type' "$schema") (plain strings: no secret reference -> AWS Q4: fetch at startup)"

step "8. AgentCore CLI: synth of the PUBLIC probe (CodeZip, existing execution role; no AWS)"
BUILD_ID="$(scripts/build-id.sh)"
echo "Build ID $BUILD_ID (endpoint b_${BUILD_ID:0:7})"
role_stub="arn:aws:iam::$(printf '1%.0s' {1..12}):role/agentcore-demo-support-exec"
MODE=PUBLIC AGENTCORE_EXECUTION_ROLE_ARN="$role_stub" bash -c '
  export AGENTCORE_NETWORK_MODE=PUBLIC AGENTCORE_TEMPORAL_ADDRESS=127.0.0.1:7233 AGENTCORE_TEMPORAL_TLS=false \
         TEMPORAL_MTLS_SECRET_ID= AGENTCORE_IDLE_SESSION_TIMEOUT=120
  scripts/agentcore-bundle.sh | tail -1 && scripts/agentcore-deploy.sh build | tail -1'
synth=build/agentcore-project/agentcore/cdk/cdk.out/AgentCore-AgentcoreDemo.template.json
echo "resources: $(jq -r '[.Resources[].Type] | join(", ")' "$synth")"
jq -r '.Resources[] | select(.Type=="AWS::BedrockAgentCore::Runtime") | .Properties
  | "runtime \(.AgentRuntimeName), \(.NetworkConfiguration.NetworkMode), \(.AgentRuntimeArtifact.CodeConfiguration.Runtime), entrypoint \(.AgentRuntimeArtifact.CodeConfiguration.EntryPoint|join(" ")), role \(if (.RoleArn|type)=="string" then "existing (executionRoleArn)" else "CREATED by the CLI" end), idle \(.LifecycleConfiguration.IdleRuntimeSessionTimeout)s, TEMPORAL_ADDRESS \(.EnvironmentVariables.TEMPORAL_ADDRESS), tags \(.Tags|tostring)"' "$synth"
jq -r '.Resources[] | select(.Type=="AWS::BedrockAgentCore::RuntimeEndpoint") | .Properties | "endpoint \(.Name) -> version \(.AgentRuntimeVersion)"' "$synth"
if jq -e '[.Resources[].Type] | index("AWS::IAM::Role")' "$synth" >/dev/null; then echo "FAIL: the CLI stack would create an IAM role"; exit 1; fi
out=build/agentcore-project/agentcore/cdk/cdk.out
zip="$out/$(jq -r '[.files[].source.path | select(endswith(".zip"))][0]' "$out/AgentCore-AgentcoreDemo.assets.json")"
echo "CodeZip asset: $(( $(wc -c <"$zip") / 1048576 )) MB; dependency file in the bundle: $(cd build/agentcore && ls pyproject.toml requirements.txt 2>/dev/null | paste -sd' ' -); arm64 wheels: $(unzip -l "$zip" | grep -c 'aarch64-linux-gnu.so')"

step "9. AgentCore sessions of ours (check of record; AWS Q8)"
scripts/agentcore-sessions.sh || { echo "FAIL: sessions of ours in the last 15 min"; exit 1; }

step "GATE: 30-agentcore-iam roles (execution role, temporal-dev-wci)"
missing=()
for r in agentcore-demo-support-exec temporal-dev-wci; do
  if aws iam get-role --role-name "$r" --query 'Role.Tags[?Key==`Project`].Value | [0]' --output text >/dev/null 2>&1; then
    echo "role $r: present (Project=$(aws iam get-role --role-name "$r" --query 'Role.Tags[?Key==`Project`].Value | [0]' --output text))"
  else echo "role $r: MISSING"; missing+=("$r"); fi
done
if [[ ${#missing[@]} -gt 0 ]]; then
  echo "BLOCKED: apply 30-agentcore-iam first: make tf-plan STACK=30-agentcore-iam TF_VAR_create_dev_wci_role=true; make tf-apply STACK=30-agentcore-iam"
  exit 3
fi
[[ "${VERIFY_PART:-2}" == 1 ]] && { printf '\nVERIFY PART 1 OK\n'; exit 0; }

# ------------------------------- Part 2: live, same-day teardown ------------------------
step "10. temporal-dev-wci profile (source_profile=default -> sts:AssumeRole)"
grep -q '^\[profile temporal-dev-wci\]' ~/.aws/config || { echo "FAIL: run make aws-profile"; exit 1; }
AWS_PROFILE=temporal-dev-wci aws sts get-caller-identity --query Arn --output text
if AWS_PROFILE=temporal-dev-wci aws bedrock-agentcore-control list-agent-runtimes --max-results 1 >/dev/null 2>&1; then
  echo "FAIL: temporal-dev-wci can do more than sts:AssumeRole"; exit 1
else echo "temporal-dev-wci cannot call AgentCore directly (only sts:AssumeRole on the invocation role): ok"; fi

step "11. Deploy the PUBLIC probe runtime (AgentCore CLI 1.0.1; ADR-0005)"
t_deploy=$(date +%s)
MODE=PUBLIC scripts/agentcore-deploy.sh deploy 2>&1 | tail -25
echo "deploy took $(( $(date +%s) - t_deploy )) s"
RUNTIME_ARN="$(scripts/agentcore-deploy.sh runtime-arn)"
ENDPOINT_ARN="$(scripts/agentcore-deploy.sh endpoint-arn)"
echo "runtime  $RUNTIME_ARN"
echo "endpoint $ENDPOINT_ARN"
rid="${RUNTIME_ARN##*/}"
aws bedrock-agentcore-control get-agent-runtime-endpoint --agent-runtime-id "$rid" --endpoint-name "b_${BUILD_ID:0:7}" \
  --query '{name: name, status: status, version: liveVersion}' --output json
aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$rid" \
  --query '{name: agentRuntimeName, status: status, network: networkConfiguration.networkMode, role: roleArn}' --output json

step "12. Invocation role: CFN stack from the vendored template (External ID from Secrets Manager)"
DEV_WCI_ARN="$($TF -chdir=infra/terraform/30-agentcore-iam output -raw dev_wci_role_arn)"
scripts/invoke-role.sh create --temporal-role-arn "$DEV_WCI_ARN" --runtime-arn "$RUNTIME_ARN"
scripts/invoke-role.sh status
if aws sts assume-role --role-arn "$(scripts/invoke-role.sh arn)" --role-session-name no-extid >/dev/null 2>&1; then
  echo "FAIL: the invocation role is assumable without the External ID"; exit 1
else echo "AssumeRole without External ID (as the SSO admin, not even trusted): denied: ok"; fi

step "13. Dev server with the WCI under AWS_PROFILE=temporal-dev-wci (WCI=aws); Finance side (scripted)"
scripts/dev-server.sh stop >/dev/null 2>&1 || true
rm -f .temporal/dev.db .temporal/dev.db-*
AWS_PROFILE=temporal-dev-wci WCI=aws scripts/dev-server.sh start
"${UV[@]}" python -m payments_mock.server >"$LOGS/payments.log" 2>&1 & pids+=($!)
"${UV[@]}" python scripts/scripted_finance_worker.py >"$LOGS/finance.log" 2>&1 & pids+=($!)
scripts/nexus-endpoint.sh apply | tail -1

step "14. Registration (local versioned Worker present) and the wrong-External-ID negative test"
"${UV[@]}" env TEMPORAL_BUILD_ID="$BUILD_ID" python -m support_agent.worker_versioned >"$LOGS/worker-versioned.log" 2>&1 & wpid=$!; pids+=("$wpid")
for _ in $(seq 1 60); do
  temporal worker deployment describe-version -n "$NS" --deployment-name "$DEP" --build-id "$BUILD_ID" >/dev/null 2>&1 && break; sleep 1
done
t_reg=$(date +%s)
TEMPORAL_BUILD_ID="$BUILD_ID" scripts/register-version.sh --agentcore-auto 2>&1 | tail -12
echo "registration took $(( $(date +%s) - t_reg )) s"
temporal worker deployment describe-version -n "$NS" --deployment-name "$DEP" --build-id "$BUILD_ID" -o json \
  | jq '{version: .versionSummary.version // .version, status: (.versionSummary.status // .status), computeConfig: (.computeConfig // .workerDeploymentVersionInfo.computeConfig // "see raw")}' 2>/dev/null \
  || temporal worker deployment describe-version -n "$NS" --deployment-name "$DEP" --build-id "$BUILD_ID"
NEG="${BUILD_ID}neg"
echo "negative: create-version $DEP:$NEG with a wrong External ID"
if out="$(with_timeout 120 temporal worker deployment create-version -n "$NS" --deployment-name "$DEP" --build-id "$NEG" \
            --aws-agentcore-endpoint-arn "$ENDPOINT_ARN" --aws-agentcore-assume-role-arn "$(scripts/invoke-role.sh arn)" \
            --aws-agentcore-assume-role-external-id "wrong-$(openssl rand -hex 4)" 2>&1)"; then
  echo "$out" | tail -3; echo "FAIL: create-version with a wrong External ID succeeded"; exit 1
fi
echo "$out" | tail -4 | cut -c1-400
temporal worker deployment delete-version -n "$NS" --deployment-name "$DEP" --build-id "$NEG" --skip-drainage >/dev/null 2>&1 || true
kill "$wpid" 2>/dev/null || true; wait "$wpid" 2>/dev/null || true
echo "local versioned Worker stopped"

step "15. Invocation probe: ping-1 with no local Worker (watch ${WATCH}s), terminate, backlog 0, invokes flat"
WCI_WF="temporal-sys-worker-controller-instance:$DEP:$BUILD_ID"
inv_count() { scripts/wci-history.sh "$BUILD_ID" 2>/dev/null | awk '/InvokeWorker activities:/{print $3}'; }
echo "WCI baseline: $(inv_count) InvokeWorker"
T_PING="$(now)"
temporal workflow start -n "$NS" --task-queue "$TQ" --type PingWorkflow -w ping-1 --input '"ping-1"' | tail -1
sleep "$WATCH"
echo "WCI events since $T_PING:"
scripts/wci-history.sh "$BUILD_ID" --since "$T_PING" | sed -n '2,200p' | grep -E 'signal|activity|failed' | tail -40
echo "InvokeWorker total now: $(inv_count)"
LOG_GROUP="/aws/bedrock-agentcore/runtimes/${rid}-b_${BUILD_ID:0:7}"
echo "AgentCore logs ($LOG_GROUP, matching lines):"
aws logs filter-log-events --log-group-name "$LOG_GROUP" --start-time "$(( $(date -j -u -f %Y-%m-%dT%H:%M:%SZ "$T_PING" +%s) * 1000 ))" \
  --filter-pattern '?"AgentCore app" ?"worker starting" ?"could not connect" ?"async task released" ?"lifetime"' \
  --query 'events[].message' --output text 2>&1 | tr '\t' '\n' | cut -c1-200 | sed -n 1,30p
temporal workflow terminate -n "$NS" -w ping-1 --reason "v0.8 probe done" >/dev/null
T_TERM="$(now)"; echo "ping-1 terminated at $T_TERM"
sleep 10
temporal task-queue describe -n "$NS" --task-queue "$TQ" -o json | jq -c '[.. | objects | select(has("approximateBacklogCount")) | {backlog: .approximateBacklogCount}] | unique' 2>/dev/null || true
c0="$(inv_count)"; sleep "$WATCH"; c1="$(inv_count)"
echo "invokes after terminate: $c0 -> $c1 over ${WATCH}s ($([[ "$c0" == "$c1" ]] && echo flat || echo NOT flat))"
echo "last InvokeWorker: $(scripts/wci-history.sh "$BUILD_ID" | grep 'InvokeWorker$' | tail -1 | awk '{print $1}')"

step "16. Q15 probe: does a Nexus completion wake the WCI? (yes / no / inconclusive)"
wf=support-2077-v08
"${UV[@]}" env TEMPORAL_BUILD_ID="$BUILD_ID" python -m support_agent.worker_versioned >>"$LOGS/worker-versioned.log" 2>&1 & wpid=$!; pids+=("$wpid")
CASE_B_WAIT=0 SUPPORT_TASK_QUEUE="$TQ" scripts/demo/case-b.sh "$wf" | grep -E '^started' || true
for _ in $(seq 1 240); do
  st="$(temporal workflow query -n agent-services -w "a2a-task-$wf" --type get_task -o json 2>/dev/null | jq -r '.queryResult[0].status.state' 2>/dev/null || echo -)"
  [[ "$st" == input-required ]] && break; sleep 1
done
echo "a2a-task-$wf: $st"
kill "$wpid" 2>/dev/null || true; wait "$wpid" 2>/dev/null || true
echo "local Worker stopped; waiting for ${WATCH}s with no invokes"
c0="$(inv_count)"; sleep "$WATCH"; c1="$(inv_count)"
echo "invokes during the quiet window: $c0 -> $c1"
T="$(now)"; echo "approve at T=$T"
scripts/demo/approve.sh "$wf" dana.finance "v0.8 Q15 probe" | tail -1
sleep 120
nexus_done="$(temporal workflow show -n "$NS" -w "$wf" -o json | jq -r '[.events[] | select(.eventType=="EVENT_TYPE_NEXUS_OPERATION_COMPLETED")][-1].eventTime')"
echo "NexusOperationCompleted in $wf: $nexus_done"
after="$(scripts/wci-history.sh "$BUILD_ID" --since "$T" | grep -E 'signal|InvokeWorker$' || true)"
echo "WCI after T:"; printf '%s\n' "$after" | sed 's/^/  /'
first_inv="$(printf '%s\n' "$after" | awk '/InvokeWorker$/{print $1; exit}')"
if [[ -n "$first_inv" && "$nexus_done" != null && "$first_inv" > "$nexus_done" ]]; then
  gap=$(( $(date -j -u -f %Y-%m-%dT%H:%M:%S "${first_inv%%.*}" +%s) - $(date -j -u -f %Y-%m-%dT%H:%M:%S "${nexus_done%%.*}" +%s) ))
  if [[ $gap -le 120 && "$c0" == "$c1" ]]; then echo "Q15 (first AWS look): YES (first invoke ${gap}s after NexusOperationCompleted, quiet window clean)"
  else echo "Q15 (first AWS look): INCONCLUSIVE (gap ${gap}s, quiet window $c0 -> $c1)"; fi
elif [[ -z "$first_inv" ]]; then echo "Q15 (first AWS look): NO (no InvokeWorker within 120 s of T)"
else echo "Q15 (first AWS look): INCONCLUSIVE (invoke not after NexusOperationCompleted)"; fi
"${UV[@]}" env TEMPORAL_BUILD_ID="$BUILD_ID" python -m support_agent.worker_versioned >>"$LOGS/worker-versioned.log" 2>&1 & wpid=$!; pids+=("$wpid")
with_timeout 300 temporal workflow result -n "$NS" -w "$wf" 2>&1 | grep -E '^ *(Status|Result)' | cut -c1-200 || true
kill "$wpid" 2>/dev/null || true; wait "$wpid" 2>/dev/null || true
for w in $(temporal workflow list -n "$NS" --query "TaskQueue='$TQ' AND ExecutionStatus='Running'" -o json | jq -r '.[].execution.workflowId'); do
  temporal workflow terminate -n "$NS" -w "$w" --reason "v0.8 cleanup" >/dev/null; echo "terminated leftover $w"
done

step "Q9: does the UI (2.54.1) show the compute config? (API the UI reads: describe-version)"
temporal worker deployment describe-version -n "$NS" --deployment-name "$DEP" --build-id "$BUILD_ID" -o json \
  | jq -c '[paths(type=="string") | join(".")] | map(select(test("(?i)compute|agentcore|scaling")))' | cut -c1-400

if [[ "${VERIFY_KEEP:-0}" == 1 ]]; then echo "VERIFY_KEEP=1: teardown skipped (run it today!)"; exit 0; fi
step "17. Same-day teardown"
temporal worker deployment set-current-version -n "$NS" --deployment-name "$DEP" --unversioned --yes 2>&1 | tail -1
temporal worker deployment delete-version -n "$NS" --deployment-name "$DEP" --build-id "$BUILD_ID" --skip-drainage 2>&1 | tail -1 || true
scripts/dev-server.sh stop
scripts/agentcore-deploy.sh down
scripts/invoke-role.sh delete
echo "waiting for the probe's sessions to idle out (120 s) and the metric to settle"
ok=0
for _ in $(seq 1 20); do
  if scripts/agentcore-sessions.sh --window 5 >"$LOGS/sessions.txt" 2>&1; then ok=1; break; fi; sleep 30
done
cat "$LOGS/sessions.txt"
[[ $ok -eq 1 ]] || { echo "FAIL: sessions of ours still starting"; exit 1; }
echo "kept: 00-guardrails ($tf00), secret temporal/agentcore-external-id, 30-agentcore-iam roles"
aws resourcegroupstaggingapi get-resources --tag-filters Key=Project,Values=agentcore-self-hosted \
  --query 'ResourceTagMappingList[].ResourceARN' --output text | tr '\t' '\n' | sed 's/^/  /'

printf '\nVERIFY OK\n'
