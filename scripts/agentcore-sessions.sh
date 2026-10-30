#!/usr/bin/env bash
# AgentCore session check of record (plan v0.8 step 0/9; AWS Q8). CloudWatch only, no session API.
#   scripts/agentcore-sessions.sh [--window MIN]     (make agentcore-sessions; default 15 min)
# Metrics (namespace AWS/Bedrock-AgentCore, confirmed with list-metrics at v0.8):
#   Sessions     {Resource=<runtime ARN>, Operation=InvokeAgentRuntime, Name=<runtime>::<endpoint>}
#                new sessions per minute, per runtime endpoint (a counter, not a gauge)
#   Invocations  {Resource, Operation, Method, Protocol}   InvokeAgentRuntime calls
#   ActiveSessionCount {Service=AgentCore.Runtime}   account-wide gauge (other teams' runtimes too)
# Our runtimes: names starting with AGENTCORE_PROJECT_NAME_ (AgentcoreDemo_). The check passes
# (exit 0, "sessions: 0") when none of ours started a session in the window, so with the
# probe's 120 s idle timeout every session of ours has ended; or when no runtime of ours exists.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
export AWS_REGION="${AWS_REGION:-us-east-1}"
PREFIX="${AGENTCORE_PROJECT_NAME:-AgentcoreDemo}_"
WINDOW=15
[[ "${1:-}" == --window ]] && WINDOW="$2"
cw() { aws --region "$AWS_REGION" cloudwatch "$@"; }
start="$(date -u -v-"${WINDOW}"M +%FT%TZ 2>/dev/null || date -u -d "-${WINDOW} min" +%FT%TZ)"
end="$(date -u +%FT%TZ)"
period=$(( (WINDOW + 1439) / 1440 * 60 ))   # GetMetricStatistics: <= 1440 datapoints

runtimes="$(aws --region "$AWS_REGION" bedrock-agentcore-control list-agent-runtimes \
  --query "agentRuntimes[?starts_with(agentRuntimeName, '$PREFIX')].agentRuntimeName" --output text)"
echo "agentcore-sessions: window ${WINDOW} min (${start} .. ${end})"
echo "  runtimes of ours (${PREFIX}*): ${runtimes:-none}"

total=0
# Every Sessions series whose endpoint Name starts with our prefix (also after the runtime
# is deleted: metrics outlive it, so a teardown check still sees the last sessions).
while IFS=$'\t' read -r resource name; do
  [[ -n "$name" ]] || continue
  sum="$(cw get-metric-statistics --namespace AWS/Bedrock-AgentCore --metric-name Sessions \
          --dimensions Name=Resource,Value="$resource" Name=Operation,Value=InvokeAgentRuntime Name=Name,Value="$name" \
          --start-time "$start" --end-time "$end" --period "$period" --statistics Sum \
          --query 'sum(Datapoints[].Sum)' --output text 2>/dev/null || echo 0)"
  [[ "$sum" == None || -z "$sum" ]] && sum=0
  sum="${sum%.*}"
  echo "  new sessions  ${name}: ${sum}"
  total=$((total + sum))
done < <(cw list-metrics --namespace AWS/Bedrock-AgentCore --metric-name Sessions \
           --query "Metrics[?length(Dimensions)==\`3\`].[Dimensions[?Name=='Resource'].Value | [0], Dimensions[?Name=='Name'].Value | [0]]" \
           --output text | awk -F'\t' -v p="$PREFIX" 'index($2, p) == 1')

active="$(cw get-metric-statistics --namespace AWS/Bedrock-AgentCore --metric-name ActiveSessionCount \
           --dimensions Name=Service,Value=AgentCore.Runtime --start-time "$start" --end-time "$end" \
           --period "$period" --statistics Maximum --query 'sort_by(Datapoints,&Timestamp)[-1].Maximum' --output text)"
echo "  account ActiveSessionCount (all runtimes, latest): ${active}"
echo "sessions: ${total}"
[[ $total -eq 0 ]]
