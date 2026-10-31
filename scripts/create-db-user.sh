#!/usr/bin/env bash
# The RDS role Temporal connects as (plan v0.11 step 2, runbook 2.2; plan 3.2: the password is
# never in git or Terraform state).
#   scripts/create-db-user.sh          create or update role temporal_user (LOGIN CREATEDB)
#   scripts/create-db-user.sh --check  read-only: secret present, role can log in over verified TLS
# What it does, idempotently:
#   1. the password: reused from Secrets Manager `temporal/rds-temporal-user` if that secret
#      exists, else `openssl rand -hex 24` and create-secret (tag Project=agentcore-self-hosted);
#   2. k8s Secret temporal/temporal-db-secret (key `password`), which the chart's
#      existingSecret reads (values.postgresql.yaml / values.eks.yaml);
#   3. CREATE/ALTER ROLE temporal_user through a throwaway postgres:16 pod in namespace temporal,
#      connecting as the RDS master user (password from the RDS-managed secret, handed to the pod
#      in a short-lived Secret that is deleted afterwards) with sslmode=verify-full against the
#      Secret rds-ca (the official us-east-1 bundle: `scripts/eks.sh rds-ca`).
# The chart's schema Job then creates the databases temporal and temporal_visibility as
# temporal_user (createDatabase: true) and owns them. Only reads .generated/foundation.env.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
export AWS_REGION=us-east-1 AWS_PAGER=""
SECRET="${DB_USER_SECRET:-temporal/rds-temporal-user}"
DB_USER=temporal_user
IMAGE=public.ecr.aws/docker/library/postgres:16
KCFG="$PWD/.temporal/kubeconfig-eks"
[[ -f .generated/foundation.env ]] || { echo "create-db-user: run 'make render-config' first" >&2; exit 1; }
# shellcheck disable=SC1091
source .generated/foundation.env
[[ -f "$KCFG" ]] || scripts/eks.sh kubeconfig >/dev/null
export KUBECONFIG="$KCFG"
k() { kubectl --context agentcore-demo-eks "$@"; }
log() { printf '[create-db-user] %s\n' "$*"; }

# Only touch the project's own database.
dbarn="$(aws rds describe-db-instances --db-instance-identifier "$RDS_IDENTIFIER" --query 'DBInstances[0].DBInstanceArn' --output text)"
tag="$(aws rds list-tags-for-resource --resource-name "$dbarn" --query "TagList[?Key=='Project'].Value | [0]" --output text)"
[[ "$tag" == agentcore-self-hosted ]] || { echo "create-db-user: $RDS_IDENTIFIER is not tagged Project=agentcore-self-hosted" >&2; exit 1; }

exists() { aws secretsmanager describe-secret --secret-id "$SECRET" >/dev/null 2>&1; }

run_pod() {  # run_pod NAME SCRIPT: psql in a throwaway pod; env from Secret db-admin-tmp; prints logs
  local name="$1" script="$2"
  k delete pod "$name" -n temporal --ignore-not-found >/dev/null
  k apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata: { name: $name, namespace: temporal, labels: { app: create-db-user } }
spec:
  restartPolicy: Never
  containers:
    - name: psql
      image: $IMAGE
      command: ["sh", "-c", $(jq -Rs . <<<"$script")]
      env:
        - { name: PGHOST, value: "$RDS_ADDRESS" }
        - { name: PGPORT, value: "$RDS_PORT" }
        - { name: PGSSLMODE, value: verify-full }
        - { name: PGSSLROOTCERT, value: /rds-ca/rds-ca.pem }
        - { name: PGCONNECT_TIMEOUT, value: "10" }
      envFrom: [{ secretRef: { name: db-admin-tmp } }]
      volumeMounts: [{ name: rds-ca, mountPath: /rds-ca, readOnly: true }]
  volumes: [{ name: rds-ca, secret: { secretName: rds-ca } }]
EOF
  local phase=""
  for _ in $(seq 1 90); do
    phase="$(k get pod "$name" -n temporal -o jsonpath='{.status.phase}' 2>/dev/null || true)"
    [[ "$phase" == Succeeded || "$phase" == Failed ]] && break
    sleep 2
  done
  k logs "$name" -n temporal 2>&1 | scripts/redact.sh | sed 's/^/  /'
  k delete pod "$name" -n temporal --ignore-not-found --wait=false >/dev/null
  [[ "$phase" == Succeeded ]]
}

tmp="$(mktemp -d)"; chmod 700 "$tmp"
cleanup() { k delete secret db-admin-tmp -n temporal --ignore-not-found >/dev/null 2>&1 || true; rm -rf "$tmp"; }
trap cleanup EXIT

k create namespace temporal --dry-run=client -o yaml | k apply -f - >/dev/null
k get secret rds-ca -n temporal >/dev/null 2>&1 || scripts/eks.sh rds-ca

if [[ "${1:-}" == --check ]]; then
  exists || { echo "create-db-user: secret $SECRET missing" >&2; exit 1; }
  aws secretsmanager get-secret-value --secret-id "$SECRET" --query SecretString --output text | tr -d '\n' >"$tmp/pw"
  k create secret generic db-admin-tmp -n temporal --from-literal=PGUSER="$DB_USER" \
    --from-file=PGPASSWORD="$tmp/pw" --dry-run=client -o yaml | k apply -f - >/dev/null
  # The role over verify-full TLS; every temporal_user session (the server pods) on TLS; schema versions.
  run_pod db-user-check "$(cat <<'EOS'
set -e
psql -d temporal -At -v ON_ERROR_STOP=1 <<'SQL'
SELECT 'this session: user=' || current_user || ' ssl=' || ssl || ' ' || version FROM pg_stat_ssl WHERE pid = pg_backend_pid();
SELECT 'temporal_user sessions: ' || count(*) || ', on TLS: ' || count(*) FILTER (WHERE s.ssl)
  FROM pg_stat_activity a JOIN pg_stat_ssl s USING (pid) WHERE a.usename = 'temporal_user';
SELECT 'schema temporal: ' || curr_version FROM schema_version;
SQL
psql -d temporal_visibility -At -v ON_ERROR_STOP=1 -c "SELECT 'schema temporal_visibility: ' || curr_version FROM schema_version"
EOS
)"
  exit $?
fi

# 1. the temporal_user password (Secrets Manager is the source of truth)
umask 077
if exists; then
  aws secretsmanager get-secret-value --secret-id "$SECRET" --query SecretString --output text | tr -d '\n' >"$tmp/pw"
  log "password: reused from Secrets Manager $SECRET"
else
  openssl rand -hex 24 | tr -d '\n' >"$tmp/pw"
  aws secretsmanager create-secret --name "$SECRET" \
    --description "RDS temporal-db role temporal_user (Temporal persistence; agentcore-self-hosted)" \
    --secret-string "file://$tmp/pw" \
    --tags Key=Project,Value=agentcore-self-hosted Key=ManagedBy,Value=scripts/create-db-user.sh >/dev/null
  log "password: generated, stored in Secrets Manager $SECRET"
fi

# 2. the k8s Secret the chart reads
k create secret generic temporal-db-secret -n temporal --from-file=password="$tmp/pw" \
  --dry-run=client -o yaml | k apply -f - >/dev/null
log "k8s Secret temporal/temporal-db-secret (key password)"

# 3. the role, as the RDS master user over verify-full TLS
aws secretsmanager get-secret-value --secret-id "$RDS_MASTER_SECRET_ARN" --query SecretString --output text \
  | jq -r .password | tr -d '\n' >"$tmp/master"
master_user="$(aws secretsmanager get-secret-value --secret-id "$RDS_MASTER_SECRET_ARN" --query SecretString --output text | jq -r .username)"
k create secret generic db-admin-tmp -n temporal --from-literal=PGUSER="$master_user" \
  --from-file=PGPASSWORD="$tmp/master" --from-file=TEMPORAL_PW="$tmp/pw" \
  --dry-run=client -o yaml | k apply -f - >/dev/null
rm -f "$tmp/master"
# psql variables (:'pw') are not expanded inside DO blocks, hence CREATE (if absent) then ALTER.
sql="$(cat <<'EOS'
set -e
cat >/tmp/user.sql <<'SQL'
SELECT 'CREATE ROLE temporal_user LOGIN CREATEDB'
  WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'temporal_user') \gexec
ALTER ROLE temporal_user WITH LOGIN CREATEDB PASSWORD :'pw';
SELECT 'role ' || rolname || ': login=' || rolcanlogin || ' createdb=' || rolcreatedb
  FROM pg_roles WHERE rolname = 'temporal_user';
SELECT 'master session: ssl=' || ssl || ' ' || version || ' (sslmode=verify-full, rds-ca bundle)'
  FROM pg_stat_ssl WHERE pid = pg_backend_pid();
SQL
psql -d postgres -At -v ON_ERROR_STOP=1 -v pw="$TEMPORAL_PW" -f /tmp/user.sql
EOS
)"
run_pod create-db-user "$sql" || { echo "create-db-user: psql failed" >&2; exit 1; }
log "role $DB_USER ready (password only in Secrets Manager $SECRET and k8s Secret temporal/temporal-db-secret)"
