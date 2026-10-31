#!/usr/bin/env bash
# Read-only pre-flight for the AWS foundation (plan v0.10 step 1; ledger 38). Never mutates.
#   VPCs and EIPs against their quotas (this project takes 1 VPC and 1 EIP; the account was at
#   4/5 and 8/10), the AgentCore AZ IDs and the node type there, the interface endpoint
#   services, the EKS cluster name, the RDS PostgreSQL minor, the CDK bootstrap stack
#   (CDKToolkit), and an IAM simulation of iam:CreateServiceLinkedRole (AgentCore VPC mode's
#   network SLR) and iam:PassRole (the Runtime execution role) for the calling principal.
# Exit 1 if anything would block `make tf-apply STACK=10-network|20-platform`.
# Output goes through scripts/redact.sh.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
exec > >(scripts/redact.sh) 2>&1
export AWS_REGION=us-east-1 AWS_PAGER=""

AZ_IDS=(use1-az1 use1-az2 use1-az4)
NODE_TYPE="${NODE_TYPE:-m7g.large}"
CLUSTER="${CLUSTER:-temporal}"
PG_VERSION="${PG_VERSION:-$(sed -n '/variable "db_engine_version"/,/^}/s/.*default *= *"\(.*\)"/\1/p' infra/terraform/20-platform/variables.tf)}"
ENDPOINTS=(bedrock-agentcore bedrock-agentcore-control sts secretsmanager bedrock-runtime logs)
fail=0
ok()  { printf '  ok    %-28s %s\n' "$1" "$2"; }
bad() { printf '  FAIL  %-28s %s\n' "$1" "$2"; fail=1; }
warn() { printf '  WARN  %-28s %s\n' "$1" "$2"; }

ours_vpc="$(aws ec2 describe-vpcs --filters Name=tag:Project,Values=agentcore-self-hosted --query 'length(Vpcs)' --output text)"
vpcs="$(aws ec2 describe-vpcs --query 'length(Vpcs)' --output text)"
vpcq="$(aws service-quotas get-service-quota --service-code vpc --quota-code L-F678F1CE --query 'Quota.Value' --output text | cut -d. -f1)"
if [[ "$ours_vpc" -gt 0 ]]; then ok vpcs "$vpcs/$vpcq (ours exists)"
elif (( vpcs < vpcq )); then ok vpcs "$vpcs/$vpcq (1 free for 10-network)"
else bad vpcs "$vpcs/$vpcq: no VPC slot left"; fi

ours_eip="$(aws ec2 describe-addresses --filters Name=tag:Project,Values=agentcore-self-hosted --query 'length(Addresses)' --output text)"
eips="$(aws ec2 describe-addresses --query 'length(Addresses)' --output text)"
eipq="$(aws service-quotas get-service-quota --service-code ec2 --quota-code L-0263D0A3 --query 'Quota.Value' --output text | cut -d. -f1)"
if [[ "$ours_eip" -gt 0 ]]; then ok eips "$eips/$eipq (ours exists: the single NAT)"
elif (( eips < eipq )); then ok eips "$eips/$eipq (1 free for the single NAT)"
else bad eips "$eips/$eipq: no EIP left"; fi

for az in "${AZ_IDS[@]}"; do
  name="$(aws ec2 describe-availability-zones --zone-ids "$az" --query 'AvailabilityZones[0].ZoneName' --output text 2>/dev/null || true)"
  [[ -n "$name" && "$name" != None ]] || { bad "az $az" "not offered"; continue; }
  off="$(aws ec2 describe-instance-type-offerings --location-type availability-zone-id \
    --filters Name=location,Values="$az" Name=instance-type,Values="$NODE_TYPE" --query 'length(InstanceTypeOfferings)' --output text)"
  [[ "$off" == 1 ]] && ok "az $az" "$name in this account; $NODE_TYPE offered" || bad "az $az" "$NODE_TYPE not offered (ADR-0006 fallback m6i.xlarge)"
done

for s in "${ENDPOINTS[@]}"; do
  azs="$(aws ec2 describe-vpc-endpoint-services --service-names "com.amazonaws.$AWS_REGION.$s" \
    --query 'ServiceDetails[0].AvailabilityZones' --output text 2>/dev/null || true)"
  [[ -n "$azs" && "$azs" != None ]] && ok "endpoint $s" "available" || bad "endpoint $s" "service not found"
done

status="$(aws eks describe-cluster --name "$CLUSTER" --query 'cluster.tags.Project' --output text 2>/dev/null || echo absent)"
case "$status" in
  absent) ok "eks name $CLUSTER" "free" ;;
  agentcore-self-hosted) ok "eks name $CLUSTER" "exists (ours)" ;;
  *) bad "eks name $CLUSTER" "taken by a cluster that is not ours" ;;
esac

pg="$(aws rds describe-db-engine-versions --engine postgres --engine-version "$PG_VERSION" --query 'DBEngineVersions[0].Status' --output text 2>/dev/null || true)"
[[ "$pg" == available ]] && ok "rds postgres $PG_VERSION" "available (Temporal tests 16.6: no longer offered)" || bad "rds postgres $PG_VERSION" "not available ($pg)"
fs="$(aws rds describe-engine-default-parameters --db-parameter-group-family postgres16 --output json \
  | jq -r '.EngineDefaults.Parameters[] | select(.ParameterName == "rds.force_ssl") | .ParameterValue')"
ok "rds.force_ssl default" "postgres16 family default = $fs (pinned to 1 in temporal-db-pg16)"

cdk="$(aws cloudformation describe-stacks --stack-name CDKToolkit --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo absent)"
case "$cdk" in
  CREATE_COMPLETE|UPDATE_COMPLETE) ok "cdk bootstrap" "CDKToolkit $cdk (the AgentCore CLI deploys through it)" ;;
  absent) bad "cdk bootstrap" "no CDKToolkit stack (npx cdk bootstrap aws://<acct>/us-east-1)" ;;
  *) warn "cdk bootstrap" "CDKToolkit $cdk" ;;
esac

# IAM simulation for the calling principal (an SSO session is an assumed role: simulate the role).
caller="$(aws sts get-caller-identity --query Arn --output text)"
acct="$(aws sts get-caller-identity --query Account --output text)"
if [[ "$caller" == *":assumed-role/"* ]]; then
  role="$(printf '%s' "$caller" | cut -d/ -f2)"
  principal="$(aws iam get-role --role-name "$role" --query 'Role.Arn' --output text)"
else
  principal="$caller"
fi
sim() {  # action resource [context-entries...]
  local action="$1" resource="$2"; shift 2
  aws iam simulate-principal-policy --policy-source-arn "$principal" --action-names "$action" \
    --resource-arns "$resource" "$@" --query 'EvaluationResults[0].EvalDecision' --output text
}
d="$(sim iam:CreateServiceLinkedRole "arn:aws:iam::$acct:role/aws-service-role/network.bedrock-agentcore.amazonaws.com/*" \
  --context-entries ContextKeyName=iam:AWSServiceName,ContextKeyValues=network.bedrock-agentcore.amazonaws.com,ContextKeyType=string)"
[[ "$d" == allowed ]] && ok "iam CreateServiceLinkedRole" "allowed (network.bedrock-agentcore SLR)" || bad "iam CreateServiceLinkedRole" "$d"
d="$(sim iam:PassRole "arn:aws:iam::$acct:role/agentcore-demo-support-exec" \
  --context-entries ContextKeyName=iam:PassedToService,ContextKeyValues=bedrock-agentcore.amazonaws.com,ContextKeyType=string)"
[[ "$d" == allowed ]] && ok "iam PassRole" "allowed (agentcore-demo-support-exec to bedrock-agentcore)" || bad "iam PassRole" "$d"
slr="$(aws iam list-roles --path-prefix /aws-service-role/network.bedrock-agentcore.amazonaws.com/ --query 'length(Roles)' --output text)"
ok "agentcore network SLR" "$( [[ "$slr" -gt 0 ]] && echo 'exists already' || echo 'absent (VPC mode creates it at v0.12)')"

[[ $fail -eq 0 ]] && echo "check-quotas: OK" || echo "check-quotas: FAILED"
exit $fail
