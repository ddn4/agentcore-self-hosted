#!/usr/bin/env bash
# Manual RDS snapshot before an install, a Helm upgrade or a schema change (runbook Phase 11
# step 1, §5 Foundation "An RDS snapshot exists from before the install"; plan v0.11).
#   scripts/rds-snapshot.sh [change]     (make rds-snapshot CHANGE=v011-install)
# Creates temporal-pre-<change>-<UTC yyyymmdd-HHMM> (tag Project=agentcore-self-hosted) and waits
# until it is available. Manual snapshots are kept until deleted (v1.0 `down-all`); storage up
# to the DB size is covered by the free backup storage allowance.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
export AWS_REGION=us-east-1 AWS_PAGER=""
DB="${RDS_IDENTIFIER:-temporal-db}"
CHANGE="${1:-change}"
[[ "$CHANGE" =~ ^[a-z0-9-]+$ ]] || { echo "rds-snapshot: change must be [a-z0-9-]+" >&2; exit 2; }
arn="$(aws rds describe-db-instances --db-instance-identifier "$DB" --query 'DBInstances[0].DBInstanceArn' --output text)"
tag="$(aws rds list-tags-for-resource --resource-name "$arn" --query "TagList[?Key=='Project'].Value | [0]" --output text)"
[[ "$tag" == agentcore-self-hosted ]] || { echo "rds-snapshot: $DB is not tagged Project=agentcore-self-hosted" >&2; exit 1; }
# yyyymmdd-HHMM: a dash, so the stamp is not a 12-digit run (redact.sh would read an account ID)
snap="temporal-pre-${CHANGE}-$(date -u +%Y%m%d-%H%M)"
aws rds create-db-snapshot --db-instance-identifier "$DB" --db-snapshot-identifier "$snap" \
  --tags Key=Project,Value=agentcore-self-hosted Key=ManagedBy,Value=scripts/rds-snapshot.sh >/dev/null
echo "rds-snapshot: creating $snap (a few minutes)"
aws rds wait db-snapshot-available --db-snapshot-identifier "$snap"
aws rds describe-db-snapshots --db-snapshot-identifier "$snap" \
  --query 'DBSnapshots[0].{id:DBSnapshotIdentifier,status:Status,engine:EngineVersion,gb:AllocatedStorage,encrypted:Encrypted}' --output json
