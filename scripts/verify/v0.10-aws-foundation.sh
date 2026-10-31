#!/usr/bin/env bash
# Observable acceptance checks for v0.10-aws-foundation (IMPLEMENTATION_PLAN.md v0.10; runbook
# §5 "Foundation" except the NLB alias, which is v0.11). us-east-1 only, default SSO profile.
# Output goes through scripts/redact.sh (account IDs, ARNs, resource IDs, IPs, EKS/RDS IDs).
#
# It never creates or deletes AWS resources: `make tf-apply STACK=10-network|20-platform` did
# that. Inside the cluster it runs short-lived test pods in a throwaway namespace
# (agentcore-demo-verify, plus temporal-workers for the IRSA check) and deletes them again.
#   1  make check (VERIFY_SKIP_CHECK=1 skips), make tf-static
#   2  make check-quotas (read-only; the VPC and EIP are now ours)
#   3  subnets on the AgentCore AZ IDs, VPC DNS support/hostnames, flow log active
#   4  interface endpoints available with private DNS, S3 gateway on the private route table
#   5  Route 53 private zone associated with the VPC
#   6  security groups: sg-agentcore-worker / sg-nlb-temporal-frontend / sg-vpce / sg-rds rules
#   7  EKS: ACTIVE, version, public endpoint = one /32 + private endpoint, OIDC provider,
#      vpc-cni enableNetworkPolicy, nodes Ready (AZ ID, arm64), LBC and cert-manager Ready
#   8  NetworkPolicy enforced by the VPC CNI agent (labelled client allowed, other blocked)
#   9  DNS inside the VPC: sts / bedrock-agentcore / bedrock-runtime resolve to private IPs
#   10 RDS: engine, class, single-AZ, encrypted, rds.force_ssl=1; psql sslmode=require from a
#      pod works (TLS in pg_stat_ssl), sslmode=disable is refused
#   11 IRSA: trust subjects of temporal-server-wci / agent-services-worker / the LBC role;
#      live: the agent-services-worker SA gets the role, another SA in the namespace does not
#   12 ECR repositories; Project tag on everything
#   13 second plan of 00/10/20/30 shows no changes
#   14 pause / resume / tf-destroy DRY_RUN (read-only)
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
exec > >(scripts/redact.sh) 2>&1
export AWS_REGION=us-east-1 AWS_PAGER=""
unset AWS_PROFILE
export KUBECONFIG="$PWD/.temporal/kubeconfig-eks"
TFO() { scripts/tf.sh output "$1" -json | jq -r "$2"; }
NS=agentcore-demo-verify
LOGS=.temporal/verify-v0.10
rm -rf "$LOGS"; mkdir -p "$LOGS"
fail=0
step() { printf '\n=== %s  (%s)\n' "$*" "$(date -u +%H:%M:%SZ)"; }
ok()  { printf '  ok    %s\n' "$*"; }
bad() { printf '  FAIL  %s\n' "$*"; fail=1; }
check() { local what="$1"; shift; if "$@"; then ok "$what"; else bad "$what"; fi; }
cleanup() {
  kubectl delete namespace "$NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  kubectl -n temporal-workers delete pod irsa-ok irsa-other --ignore-not-found >/dev/null 2>&1 || true
  if [[ "${CREATED_TW:-0}" == 1 ]]; then kubectl delete namespace temporal-workers --ignore-not-found --wait=false >/dev/null 2>&1 || true; fi
  rm -f infra/terraform/*/tfplan
}
trap cleanup EXIT

# --- 1 ---------------------------------------------------------------------------------
step "1. make check, make tf-static"
if [[ "${VERIFY_SKIP_CHECK:-0}" == 1 ]]; then echo "skipped (VERIFY_SKIP_CHECK=1)"
else make check >$LOGS/check.log 2>&1 && ok "make check" || { tail -20 $LOGS/check.log; bad "make check"; }; fi
make tf-static 2>&1 | grep -E 'fmt ok|tflint ok|valid' | sort | uniq -c

# --- 2 ---------------------------------------------------------------------------------
step "2. make check-quotas (read-only)"
scripts/check-quotas.sh || bad "check-quotas"

# --- 3 ---------------------------------------------------------------------------------
step "3. Subnets by AZ ID, VPC DNS, flow log"
VPC="$(TFO 10-network .vpc_id.value)"
SUBNETS="$(TFO 10-network '.private_subnet_ids.value | join(" ")')"
aws ec2 describe-subnets --subnet-ids $SUBNETS --query 'Subnets[].[SubnetId,AvailabilityZoneId,AvailabilityZone,CidrBlock]' --output text
azids="$(aws ec2 describe-subnets --subnet-ids $SUBNETS --query 'Subnets[].AvailabilityZoneId' --output text | tr '\t' '\n' | sort | tr '\n' ' ')"
check "private subnets on use1-az1 use1-az2 use1-az4 (got: $azids)" [ "$azids" == "use1-az1 use1-az2 use1-az4 " ]
dnss="$(aws ec2 describe-vpc-attribute --vpc-id "$VPC" --attribute enableDnsSupport --query 'EnableDnsSupport.Value' --output text)"
dnsh="$(aws ec2 describe-vpc-attribute --vpc-id "$VPC" --attribute enableDnsHostnames --query 'EnableDnsHostnames.Value' --output text)"
check "VPC enableDnsSupport=$dnss enableDnsHostnames=$dnsh" [ "$dnss/$dnsh" == "True/True" ]
fl="$(aws ec2 describe-flow-logs --filter Name=resource-id,Values="$VPC" --query 'FlowLogs[0].[FlowLogStatus,TrafficType,LogDestinationType,MaxAggregationInterval]' --output text)"
check "flow log: $fl" grep -q '^ACTIVE	ALL	cloud-watch-logs' <<<"$fl"
nats="$(aws ec2 describe-nat-gateways --filter Name=vpc-id,Values="$VPC" Name=state,Values=available --query 'length(NatGateways)' --output text)"
check "NAT gateways available: $nats (single NAT, one EIP)" [ "$nats" == 1 ]

# --- 4 ---------------------------------------------------------------------------------
step "4. VPC endpoints"
aws ec2 describe-vpc-endpoints --filters Name=vpc-id,Values="$VPC" \
  --query 'VpcEndpoints[].[ServiceName,VpcEndpointType,State,PrivateDnsEnabled,length(SubnetIds)]' --output text | sort
for s in bedrock-agentcore bedrock-agentcore-control sts secretsmanager bedrock-runtime logs; do
  st="$(aws ec2 describe-vpc-endpoints --filters Name=vpc-id,Values="$VPC" Name=service-name,Values="com.amazonaws.us-east-1.$s" \
    --query 'VpcEndpoints[0].[State,PrivateDnsEnabled]' --output text)"
  check "interface endpoint $s: $st" [ "$st" == "available	True" ]
done
RTB="$(TFO 10-network '.private_route_table_ids.value[0]')"
s3="$(aws ec2 describe-vpc-endpoints --filters Name=vpc-id,Values="$VPC" Name=service-name,Values=com.amazonaws.us-east-1.s3 \
  --query 'VpcEndpoints[0].[VpcEndpointType,State,join(`,`,RouteTableIds)]' --output text)"
check "S3 gateway endpoint on the private route table: $s3" [ "$s3" == "Gateway	available	$RTB" ]

# --- 5 ---------------------------------------------------------------------------------
step "5. Route 53 private zone"
ZONE="$(TFO 10-network .private_zone_id.value)"
aws route53 get-hosted-zone --id "$ZONE" --query '{name:HostedZone.Name,private:HostedZone.Config.PrivateZone,vpcs:VPCs}' --output json
assoc="$(aws route53 get-hosted-zone --id "$ZONE" --query "VPCs[?VPCId=='$VPC'].VPCRegion | [0]" --output text)"
check "zone temporal-customer-support.internal is private and associated with the VPC ($assoc)" [ "$assoc" == us-east-1 ]

# --- 6 ---------------------------------------------------------------------------------
step "6. Security groups (runbook 1.5)"
for role in agentcore_worker nlb_temporal_frontend vpce rds; do
  id="$(TFO 10-network ".security_group_ids.value.$role")"
  name="$(aws ec2 describe-security-groups --group-ids "$id" --query 'SecurityGroups[0].[GroupName,Tags[?Key==`Role`].Value|[0]]' --output text)"
  echo "-- $name"
  aws ec2 describe-security-group-rules --filters Name=group-id,Values="$id" \
    --query 'SecurityGroupRules[].[IsEgress,IpProtocol,FromPort,ToPort,ReferencedGroupInfo.GroupId||CidrIpv4||PrefixListId,Description]' --output text \
    | sed -e 's/^True/  egress /' -e 's/^False/  ingress/'
done
AC="$(TFO 10-network .security_group_ids.value.agentcore_worker)"
acin="$(aws ec2 describe-security-group-rules --filters Name=group-id,Values="$AC" --query 'length(SecurityGroupRules[?!IsEgress])' --output text)"
acout="$(aws ec2 describe-security-group-rules --filters Name=group-id,Values="$AC" --query 'length(SecurityGroupRules[?IsEgress])' --output text)"
check "sg-agentcore-worker: no ingress, 3 egress rules (7233 NLB, 443 sg-vpce, 443 S3 prefix list)" [ "$acin/$acout" == "0/3" ]

# --- 7 ---------------------------------------------------------------------------------
step "7. EKS"
scripts/eks.sh kubeconfig
CLUSTER="$(TFO 20-platform .cluster_name.value)"
aws eks describe-cluster --name "$CLUSTER" --query 'cluster.{status:status,version:version,platform:platformVersion,
  publicAccess:resourcesVpcConfig.endpointPublicAccess,privateAccess:resourcesVpcConfig.endpointPrivateAccess,
  publicCidrs:resourcesVpcConfig.publicAccessCidrs,authMode:accessConfig.authenticationMode,logging:logging.clusterLogging[0].types}' --output json
cidrs="$(aws eks describe-cluster --name "$CLUSTER" --query 'cluster.resourcesVpcConfig.publicAccessCidrs' --output text)"
laptop="$(curl -s https://checkip.amazonaws.com | tr -d '[:space:]')/32"
check "public endpoint limited to exactly the laptop's /32" [ "$cidrs" == "$laptop" ]
issuer="$(aws eks describe-cluster --name "$CLUSTER" --query 'cluster.identity.oidc.issuer' --output text)"
oidc="$(aws iam list-open-id-connect-providers --query "OpenIDConnectProviderList[?ends_with(Arn, '${issuer#https://oidc.eks.us-east-1.amazonaws.com/id/}')].Arn | [0]" --output text)"
check "IAM OIDC provider for the cluster issuer exists (IRSA)" [ "$oidc" != None ]
cni="$(aws eks describe-addon --cluster-name "$CLUSTER" --addon-name vpc-cni --query 'addon.[addonVersion,status,configurationValues]' --output text)"
echo "vpc-cni: $cni"
check "vpc-cni add-on ACTIVE with enableNetworkPolicy=true" grep -q 'ACTIVE.*"enableNetworkPolicy":"true"' <<<"$cni"
for a in coredns kube-proxy; do
  echo "$a: $(aws eks describe-addon --cluster-name "$CLUSTER" --addon-name $a --query 'addon.[addonVersion,status]' --output text)"
done
kubectl get nodes -L topology.k8s.aws/zone-id,kubernetes.io/arch,node.kubernetes.io/instance-type
ready="$(kubectl get nodes -o json | jq '[.items[] | select(.status.conditions[] | select(.type=="Ready" and .status=="True"))] | length')"
arm="$(kubectl get nodes -l kubernetes.io/arch=arm64 -o name | wc -l | tr -d ' ')"
check "nodes Ready: $ready (>= 2), arm64: $arm" [ "$ready" -ge 2 -a "$arm" == "$ready" ]
nodeaz="$(kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.labels.topology\.k8s\.aws/zone-id}{"\n"}{end}' | sort -u | tr '\n' ' ')"
offaz="$(tr ' ' '\n' <<<"$nodeaz" | sed '/^$/d' | grep -vE '^(use1-az1|use1-az2|use1-az4)$' || true)"
check "nodes only on AgentCore AZ IDs ($nodeaz)" [ -z "$offaz" ]
npa="$(kubectl -n kube-system get ds aws-node -o jsonpath='{.spec.template.spec.containers[*].name}')"
check "aws-node runs the network policy agent aws-eks-nodeagent (containers: $npa)" grep -q aws-eks-nodeagent <<<"$npa"
check "CRD policyendpoints.networking.k8s.aws present" kubectl get crd policyendpoints.networking.k8s.aws -o name
kubectl -n kube-system get deploy aws-load-balancer-controller
check "LBC Available" kubectl -n kube-system wait deploy/aws-load-balancer-controller --for=condition=Available --timeout=120s
lbcsa="$(kubectl -n kube-system get sa aws-load-balancer-controller -o jsonpath='{.metadata.annotations.eks\.amazonaws\.com/role-arn}')"
check "LBC ServiceAccount annotated with its IRSA role ($lbcsa)" grep -q 'role/temporal-aws-load-balancer-controller$' <<<"$lbcsa"
kubectl -n cert-manager get deploy
check "cert-manager Available (controller, cainjector, webhook)" kubectl -n cert-manager wait deploy --all --for=condition=Available --timeout=120s
helm --kubeconfig "$KUBECONFIG" list -A

# --- 8 ---------------------------------------------------------------------------------
step "8. NetworkPolicy enforced by the VPC CNI network-policy agent"
IMG=public.ecr.aws/docker/library/busybox:1.37
kubectl create namespace "$NS" >/dev/null
kubectl -n "$NS" apply -f - >/dev/null <<EOF
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: web-allow-labelled}
spec:
  podSelector: {matchLabels: {app: web}}
  policyTypes: [Ingress]
  ingress:
    - from: [{podSelector: {matchLabels: {access: allowed}}}]
      ports: [{protocol: TCP, port: 8080}]
EOF
kubectl -n "$NS" run web --image="$IMG" --labels=app=web --restart=Never -- sh -c 'echo ok >/tmp/index.html; httpd -f -p 8080 -h /tmp' >/dev/null
kubectl -n "$NS" wait pod/web --for=condition=Ready --timeout=180s >/dev/null
WEB="$(kubectl -n "$NS" get pod web -o jsonpath='{.status.podIP}')"
sleep 10  # the agent programs the policy for the new pod
probe() {  # name labels -> prints the wget result
  kubectl -n "$NS" run "$1" --image="$IMG" --labels="$2" --restart=Never --rm -i --quiet --command -- \
    sh -c "wget -q -T 5 -O - http://$WEB:8080/ 2>&1 || echo BLOCKED" 2>/dev/null | tr -d '\r' | tail -1
}
r1="$(probe client-allowed access=allowed)"; r2="$(probe client-other access=none)"
check "labelled client reaches web:8080 (got: $r1)" [ "$r1" == ok ]
check "unlabelled client is blocked (got: $r2)" [ "$r2" == BLOCKED ]

# --- 9 ---------------------------------------------------------------------------------
step "9. DNS from inside the VPC: AWS APIs resolve to the interface endpoints"
dns="$(kubectl -n "$NS" run dns --image="$IMG" --restart=Never --rm -i --quiet --command -- sh -c '
  for h in sts bedrock-agentcore bedrock-agentcore-control bedrock-runtime secretsmanager logs; do
    ip=$(nslookup $h.us-east-1.amazonaws.com 2>/dev/null | awk "/^Address: /{print \$2}" | head -1); echo "$h $ip"
  done' 2>/dev/null | tr -d '\r' | sort -u)"
while read -r h ip; do
  [[ -n "$h" ]] || continue
  if [[ "$ip" == 10.40.* ]]; then ok "$h.us-east-1.amazonaws.com -> 10.40.x.x (endpoint ENI, private DNS)"; else bad "$h resolves to ${ip:-nothing}"; fi
done <<<"$dns"

# --- 10 --------------------------------------------------------------------------------
step "10. RDS PostgreSQL"
DB="$(TFO 20-platform .rds_identifier.value)"
aws rds describe-db-instances --db-instance-identifier "$DB" --query 'DBInstances[0].{status:DBInstanceStatus,engine:Engine,
  version:EngineVersion,class:DBInstanceClass,multiAZ:MultiAZ,encrypted:StorageEncrypted,storage:StorageType,
  public:PubliclyAccessible,deletionProtection:DeletionProtection,ca:CACertificateIdentifier,
  autoMinor:AutoMinorVersionUpgrade,paramGroup:DBParameterGroups[0].DBParameterGroupName,managedSecret:MasterUserSecret.SecretStatus}' --output json
st="$(aws rds describe-db-instances --db-instance-identifier "$DB" --query 'DBInstances[0].[DBInstanceStatus,EngineVersion,DBInstanceClass,MultiAZ,StorageEncrypted,DeletionProtection]' --output text)"
check "RDS available, 16.x, db.t4g.medium, single-AZ, encrypted, deletion_protection=false ($st)" grep -qE '^available	16\.[0-9]+	db\.t4g\.medium	False	True	False$' <<<"$st"
pg="$(aws rds describe-db-instances --db-instance-identifier "$DB" --query 'DBInstances[0].DBParameterGroups[0].DBParameterGroupName' --output text)"
fs="$(aws rds describe-db-parameters --db-parameter-group-name "$pg" --output json | jq -r '.Parameters[] | select(.ParameterName=="rds.force_ssl") | .ParameterValue')"
check "parameter group $pg: rds.force_ssl=$fs" [ "$fs" == 1 ]
HOST="$(TFO 20-platform .rds_address.value)"
SECRET="$(TFO 20-platform .rds_master_secret_arn.value)"
# rds.force_ssl is a parameter-group setting, not a GUC visible to SQL (current_setting fails);
# a value equal to the family default is reported with Source=system.
# The master password goes from Secrets Manager into a throwaway Secret, never onto a command line.
kubectl -n "$NS" create secret generic pg --from-env-file=<(
  aws secretsmanager get-secret-value --secret-id "$SECRET" --query SecretString --output text \
    | jq -r '"PGUSER=\(.username)\nPGPASSWORD=\(.password)"') >/dev/null
psql_pod() {  # name sslmode sql
  kubectl -n "$NS" apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata: {name: $1}
spec:
  restartPolicy: Never
  containers:
    - name: psql
      image: public.ecr.aws/docker/library/postgres:16
      envFrom: [{secretRef: {name: pg}}]
      env: [{name: PGCONNECT_TIMEOUT, value: "10"}]
      command: [psql, "host=$HOST dbname=postgres sslmode=$2", -XAtc, "$3"]
EOF
  for _ in $(seq 1 60); do
    phase="$(kubectl -n "$NS" get pod "$1" -o jsonpath='{.status.phase}')"
    [[ "$phase" == Succeeded || "$phase" == Failed ]] && break; sleep 3
  done
  kubectl -n "$NS" logs "$1" 2>&1 | tr -d '\r'
  [[ "$phase" == Succeeded ]]
}
out="$(psql_pod psql-tls require "select 'ssl='||ssl||' version='||version||' cipher='||cipher from pg_stat_ssl where pid = pg_backend_pid() union all select 'server='||current_setting('server_version')")" \
  && ok "psql sslmode=require from a pod in the cluster" || bad "psql sslmode=require failed"
echo "$out" | sed 's/^/    /'
check "the session is TLS (pg_stat_ssl.ssl = true)" grep -qE '^ssl=(t|true) ' <<<"$out"
out2="$(psql_pod psql-plain disable "select 1" || true)"
echo "$out2" | sed 's/^/    /'
check "sslmode=disable is refused (force_ssl)" grep -qiE 'no encryption|SSL off|pg_hba' <<<"$out2"

# --- 11 --------------------------------------------------------------------------------
step "11. IRSA trust subjects"
for r in temporal-server-wci agent-services-worker temporal-aws-load-balancer-controller; do
  subj="$(aws iam get-role --role-name "$r" --query 'Role.AssumeRolePolicyDocument.Statement[0].Condition.StringEquals' --output json \
    | jq -r 'to_entries[] | select(.key|endswith(":sub")) | .value | if type=="array" then join(",") else . end')"
  echo "$r: sub=$subj"
done
check "temporal-server-wci trusts only system:serviceaccount:temporal:temporal-worker" \
  bash -c '[[ "$(aws iam get-role --role-name temporal-server-wci --query "Role.AssumeRolePolicyDocument.Statement[0].Condition.StringEquals" --output json | jq -r "to_entries[] | select(.key|endswith(\":sub\")) | .value")" == system:serviceaccount:temporal:temporal-worker ]]'
check "agent-services-worker trusts only system:serviceaccount:temporal-workers:agent-services-worker" \
  bash -c '[[ "$(aws iam get-role --role-name agent-services-worker --query "Role.AssumeRolePolicyDocument.Statement[0].Condition.StringEquals" --output json | jq -r "to_entries[] | select(.key|endswith(\":sub\")) | .value")" == system:serviceaccount:temporal-workers:agent-services-worker ]]'
echo "-- temporal-server-wci policy:"; aws iam get-role-policy --role-name temporal-server-wci --policy-name temporal-server-wci --query 'PolicyDocument.Statement' --output json | jq -c '.[]'
echo "-- agent-services-worker policy:"; aws iam get-role-policy --role-name agent-services-worker --policy-name agent-services-worker --query 'PolicyDocument.Statement' --output json | jq -c '.[]'
# Live IRSA: the annotated agent-services-worker SA gets the role; another SA with the same
# annotation in the same namespace is refused by the trust policy's sub condition.
ROLE="$(TFO 20-platform .agent_services_worker_role_arn.value)"
if ! kubectl get namespace temporal-workers >/dev/null 2>&1; then kubectl create namespace temporal-workers >/dev/null; CREATED_TW=1; fi
for sa in agent-services-worker irsa-other; do
  kubectl -n temporal-workers create serviceaccount "$sa" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl -n temporal-workers annotate sa "$sa" --overwrite "eks.amazonaws.com/role-arn=$ROLE" >/dev/null
done
irsa_pod() {  # pod sa
  kubectl -n temporal-workers run "$1" --image=public.ecr.aws/aws-cli/aws-cli:2.36.46 --restart=Never --rm -i --quiet \
    --overrides="{\"spec\":{\"serviceAccountName\":\"$2\"}}" -- sts get-caller-identity --query Arn --output text 2>&1 | tr -d '\r' | grep -v '^pod .* terminated' | tail -1 || true
}
a1="$(irsa_pod irsa-ok agent-services-worker)"; a2="$(irsa_pod irsa-other irsa-other)"
echo "agent-services-worker SA: $a1"; echo "irsa-other SA: $a2"
check "the agent-services-worker SA assumes the role (AssumeRoleWithWebIdentity)" grep -q 'assumed-role/agent-services-worker/' <<<"$a1"
check "another SA with the same annotation is refused (trust sub)" grep -q 'AccessDenied' <<<"$a2"
if [[ "${CREATED_TW:-0}" == 1 ]]; then kubectl delete namespace temporal-workers --wait=false >/dev/null; CREATED_TW=0
else kubectl -n temporal-workers delete sa agent-services-worker irsa-other >/dev/null; fi

# --- 12 --------------------------------------------------------------------------------
step "12. ECR and the Project tag"
aws ecr describe-repositories --repository-names agentcore-demo/finance-agent agentcore-demo/payments-mock \
  --query 'repositories[].[repositoryName,imageScanningConfiguration.scanOnPush,imageTagMutability]' --output text
echo "resources tagged Project=agentcore-self-hosted, by service:"
aws resourcegroupstaggingapi get-resources --tag-filters Key=Project,Values=agentcore-self-hosted \
  --query 'ResourceTagMappingList[].ResourceARN' --output text | tr '\t' '\n' | cut -d: -f3 | sort | uniq -c

# --- 13 --------------------------------------------------------------------------------
step "13. Second plan: no changes"
for s in 00-guardrails 10-network 20-platform 30-agentcore-iam; do
  res="$(scripts/tf.sh plan "$s" -detailed-exitcode >$LOGS/plan-$s.log 2>&1; echo $?)"
  rm -f "infra/terraform/$s/tfplan"
  if [[ "$res" == 0 ]]; then ok "$s: No changes"; else bad "$s: plan exit $res"; grep -E '^  # |Error' $LOGS/plan-$s.log | head; fi
done

# --- 14 --------------------------------------------------------------------------------
step "14. pause / resume / tf-destroy, dry runs only (nothing changes)"
DRY_RUN=1 scripts/pause.sh pause
DRY_RUN=1 scripts/pause.sh resume
DRY_RUN=1 scripts/tf.sh destroy foundation

echo
[[ $fail -eq 0 ]] && echo "v0.10-aws-foundation verify: OK" || echo "v0.10-aws-foundation verify: FAILED"
exit $fail
