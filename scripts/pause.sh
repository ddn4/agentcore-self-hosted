#!/usr/bin/env bash
# Pause / resume the AWS foundation between sessions (plan v0.10 "Pause and rollback").
#   scripts/pause.sh pause    (make pause)   node group to 0 (min 0, desired 0), stop RDS
#   scripts/pause.sh resume   (make resume)  node group back to min/desired 2, start RDS
#   DRY_RUN=1 ...             read-only: print the current state and the commands it would run
# Still billed while paused: the EKS control plane, the NAT gateway + EIP, the interface
# endpoints, RDS storage, logs (~$0.28/h, ADR-0006). AWS starts a
# stopped RDS instance again after 7 days by itself. `make pause` leaves the node group's
# min_size at 0, which Terraform reports as drift until `make resume`.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
export AWS_REGION=us-east-1 AWS_PAGER=""
action="${1:-}"; [[ "$action" == pause || "$action" == resume ]] || { echo "usage: $0 pause|resume" >&2; exit 2; }
DRY="${DRY_RUN:-0}"

out() { scripts/tf.sh output 20-platform -raw "$1"; }
cluster="$(out cluster_name)"
nodegroup="$(out node_group_name)"
db="$(out rds_identifier)"
min="${NODE_MIN:-2}" desired="${NODE_DESIRED:-2}" max="${NODE_MAX:-3}"

# Only act on what this project created.
tag="$(aws eks describe-nodegroup --cluster-name "$cluster" --nodegroup-name "$nodegroup" --query 'nodegroup.tags.Project' --output text)"
[[ "$tag" == agentcore-self-hosted ]] || { echo "pause: node group $nodegroup is not tagged Project=agentcore-self-hosted" >&2; exit 1; }
dbarn="$(aws rds describe-db-instances --db-instance-identifier "$db" --query 'DBInstances[0].DBInstanceArn' --output text)"
dbtag="$(aws rds list-tags-for-resource --resource-name "$dbarn" --query "TagList[?Key=='Project'].Value | [0]" --output text)"
[[ "$dbtag" == agentcore-self-hosted ]] || { echo "pause: RDS $db is not tagged Project=agentcore-self-hosted" >&2; exit 1; }

echo "now: node group $nodegroup scaling $(aws eks describe-nodegroup --cluster-name "$cluster" --nodegroup-name "$nodegroup" \
  --query 'nodegroup.scalingConfig' --output json | tr -d ' \n'); RDS $db $(aws rds describe-db-instances \
  --db-instance-identifier "$db" --query 'DBInstances[0].DBInstanceStatus' --output text)"

if [[ "$action" == pause ]]; then
  cmds=(
    "aws eks update-nodegroup-config --cluster-name $cluster --nodegroup-name $nodegroup --scaling-config minSize=0,maxSize=$max,desiredSize=0"
    "aws rds stop-db-instance --db-instance-identifier $db"
  )
else
  cmds=(
    "aws rds start-db-instance --db-instance-identifier $db"
    "aws eks update-nodegroup-config --cluster-name $cluster --nodegroup-name $nodegroup --scaling-config minSize=$min,maxSize=$max,desiredSize=$desired"
  )
fi
for c in "${cmds[@]}"; do
  if [[ "$DRY" == 1 ]]; then echo "DRY_RUN: $c"; else echo "+ $c"; $c --output json >/dev/null; fi
done
[[ "$DRY" == 1 ]] && echo "DRY_RUN: nothing changed" || echo "$action: requested (RDS takes ~5 min; nodes ~3 min)"
