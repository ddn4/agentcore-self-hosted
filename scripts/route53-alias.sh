#!/usr/bin/env bash
# Route 53 alias for the Temporal frontend NLB (plan v0.11; runbook 2.4 "Route 53 alias
# (mandatory)"; no external-dns in this demo).
#   scripts/route53-alias.sh           UPSERT A (alias) temporal-frontend.temporal-customer-support.internal
#                                      -> the internal NLB temporal-frontend, wait for INSYNC
#   scripts/route53-alias.sh --show    print the record (redacted)
#   scripts/route53-alias.sh --delete  DELETE the record (rollback, before helm uninstall)
# Guards: the zone must be the project's private zone (render-config), and the NLB must carry
# Project=agentcore-self-hosted (Service annotation aws-load-balancer-additional-resource-tags).
# The UI stays on a port-forward (runbook 2.8 "simplest option for the demo"): no UI record.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
export AWS_REGION=us-east-1 AWS_PAGER=""
[[ -f .generated/foundation.env ]] || { echo "route53-alias: run 'make render-config' first" >&2; exit 1; }
# shellcheck disable=SC1091
source .generated/foundation.env
NAME="temporal-frontend.${PRIVATE_ZONE_NAME}"
NLB_NAME="${NLB_NAME:-temporal-frontend}"

zone_name="$(aws route53 get-hosted-zone --id "$PRIVATE_ZONE_ID" --query 'HostedZone.[Name,Config.PrivateZone]' --output text)"
[[ "$zone_name" == "${PRIVATE_ZONE_NAME}."$'\t'"True" ]] || { echo "route53-alias: zone is not the private zone $PRIVATE_ZONE_NAME" >&2; exit 1; }

record() {
  aws route53 list-resource-record-sets --hosted-zone-id "$PRIVATE_ZONE_ID" \
    --query "ResourceRecordSets[?Name=='${NAME}.' && Type=='A'] | [0]" --output json
}

change() {  # change ACTION DNS ZONE
  local batch id
  batch="$(jq -cn --arg a "$1" --arg n "$NAME" --arg d "$2" --arg z "$3" \
    '{Comment: "agentcore-self-hosted v0.11 frontend alias", Changes: [{Action: $a, ResourceRecordSet: {Name: $n, Type: "A",
      AliasTarget: {HostedZoneId: $z, DNSName: $d, EvaluateTargetHealth: false}}}]}')"
  id="$(aws route53 change-resource-record-sets --hosted-zone-id "$PRIVATE_ZONE_ID" --change-batch "$batch" --query ChangeInfo.Id --output text)"
  aws route53 wait resource-record-sets-changed --id "$id"
  echo "route53-alias: $1 $NAME -> NLB $NLB_NAME: INSYNC"
}

case "${1:-upsert}" in
  upsert)
    read -r arn dns zone < <(aws elbv2 describe-load-balancers --names "$NLB_NAME" \
      --query 'LoadBalancers[0].[LoadBalancerArn,DNSName,CanonicalHostedZoneId]' --output text)
    tag="$(aws elbv2 describe-tags --resource-arns "$arn" --query "TagDescriptions[0].Tags[?Key=='Project'].Value | [0]" --output text)"
    [[ "$tag" == agentcore-self-hosted ]] || { echo "route53-alias: NLB $NLB_NAME is not tagged Project=agentcore-self-hosted" >&2; exit 1; }
    scheme="$(aws elbv2 describe-load-balancers --load-balancer-arns "$arn" --query 'LoadBalancers[0].Scheme' --output text)"
    [[ "$scheme" == internal ]] || { echo "route53-alias: NLB $NLB_NAME is $scheme, not internal" >&2; exit 1; }
    change UPSERT "$dns" "$zone"
    ;;
  --show) record | scripts/redact.sh ;;
  --delete)
    cur="$(record)"
    if [[ "$cur" == null ]]; then echo "route53-alias: no record $NAME"; exit 0; fi
    change DELETE "$(jq -r .AliasTarget.DNSName <<<"$cur")" "$(jq -r .AliasTarget.HostedZoneId <<<"$cur")"
    ;;
  *) echo "usage: $0 [upsert|--show|--delete]" >&2; exit 2 ;;
esac
