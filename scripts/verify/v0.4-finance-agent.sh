#!/usr/bin/env bash
# Observable acceptance checks for v0.4-finance-agent (IMPLEMENTATION_PLAN.md v0.4).
# Output is piped through scripts/redact.sh so it can be pasted into the story doc.
#   VERIFY_SKIP_BEDROCK=1  Finance runs a scripted model (no AWS); otherwise Opus 5.5 (a few
#                          small calls: one A2A task = 2 model turns)
#   RECORD_HISTORY=1       re-record replay fixtures (smoke-1042 caller + a2a-task-smoke-1042)
#   VERIFY_SKIP_CHECK=1    skip step 1
# Starts its own dev server (unless one is running), payments-mock, Finance Worker and smoke
# Worker, and stops everything it started on exit.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
exec > >(scripts/redact.sh) 2>&1

export TEMPORAL_CONFIG_FILE="$PWD/config/temporal.toml" TEMPORAL_PROFILE=local
UV=(uv run --frozen)
PY=.venv/bin/python
LOGS=.temporal/verify-v0.4
rm -rf "$LOGS"; mkdir -p "$LOGS"
RECORD="${RECORD_HISTORY:-0}"
PAYMENTS=http://127.0.0.1:8081
export PAYMENTS_URL=$PAYMENTS

step() { printf '\n=== %s\n' "$*"; }
started_server=0
pids=()
cleanup() {
  for p in "${pids[@]:-}"; do [[ -n "$p" ]] && kill "$p" 2>/dev/null || true; done
  for p in "${pids[@]:-}"; do [[ -n "$p" ]] && wait "$p" 2>/dev/null || true; done
  [[ $started_server -eq 1 ]] && scripts/dev-server.sh stop || true
}
trap cleanup EXIT

step "1. make check (lint, types, unit, workflow, integration, infra-static, scrub-check)"
if [[ "${VERIFY_SKIP_CHECK:-0}" == "1" ]]; then
  echo "SKIPPED (VERIFY_SKIP_CHECK=1)"
else
  make -s check >"$LOGS/check.log" 2>&1 || { cat "$LOGS/check.log"; exit 1; }
  grep -E "passed|0 errors|infra-static|scrub-check|make check" "$LOGS/check.log"
fi

step "2. contract: round-trips (default + pydantic converters), 7b.9 CLI JSON, schema gate, card"
"${UV[@]}" pytest -q packages/finance-a2a-api 2>&1 | tail -1

step "3. spike: the A2A hop under the time-skipping test servers"
"${UV[@]}" python spikes/nexus_time_skipping.py 2>&1 | grep -E '^time-skipping'

step "4. dev server (both namespaces; standalone Nexus operations enabled)"
if scripts/dev-server.sh status >/dev/null 2>&1; then
  echo "dev server already running; using it (needs the v0.4 dev-server.sh flags for step 7)"
else
  scripts/dev-server.sh start
  started_server=1
fi

step "5. payments-mock, Finance Worker (agent-services), smoke Worker (agents/nexus-smoke-tq)"
"$PY" -m payments_mock.server >"$LOGS/payments.log" 2>&1 & pids+=("$!")
if [[ "${VERIFY_SKIP_BEDROCK:-0}" == "1" ]]; then
  echo "Finance model: scripted (VERIFY_SKIP_BEDROCK=1)"
  "$PY" scripts/scripted_finance_worker.py --delay 0.5 >"$LOGS/finance.log" 2>&1 & pids+=("$!")
else
  echo "Finance model: Opus 5.5 (make worker-finance)"
  "$PY" -m finance_agent.worker >"$LOGS/finance.log" 2>&1 & pids+=("$!")
fi
"$PY" -m support_agent.smoke_worker >"$LOGS/smoke.log" 2>&1 & pids+=("$!")
for _ in $(seq 1 40); do curl -sf "$PAYMENTS/healthz" >/dev/null && break; sleep 0.25; done
curl -sf "$PAYMENTS/healthz"; echo
sleep 3
grep -hE "finance worker|smoke worker|payments-mock listening" "$LOGS"/*.log | sed -E 's/^[0-9-]+ [0-9:,]+ //'
temporal task-queue describe --namespace agent-services --task-queue agent-services-tq -o json \
  | jq -r '"agent-services-tq pollers: \([.pollers[]?] | length)"' 2>/dev/null || true

step "6. make nexus-endpoint, twice (idempotent); description = Agent Card"
scripts/nexus-endpoint.sh apply
scripts/nexus-endpoint.sh apply
temporal operator nexus endpoint get --name finance-agent -o json \
  | jq -r '.spec.target.worker | "target: \(.namespace)/\(.taskQueue)"'
"${UV[@]}" python - <<'EOF'
import asyncio
from temporalio.api.operatorservice.v1 import ListNexusEndpointsRequest
from demo_testing.nexus import endpoint_description
from finance_a2a_api import render_markdown
from support_agent import config
async def main() -> None:
    client = await config.connect()
    resp = await client.operator_service.list_nexus_endpoints(ListNexusEndpointsRequest(name="finance-agent"))
    desc = endpoint_description(resp.endpoints[0])
    assert desc == render_markdown(), "endpoint description != FINANCE_AGENT_CARD markdown"
    print("endpoint description == render_markdown(FINANCE_AGENT_CARD):", desc.splitlines()[0])
asyncio.run(main())
EOF
# What the Web UI's Nexus page fetches: the UI server's own API (json/plain payload).
"${UV[@]}" python - <<'EOF'
import base64, json, os, urllib.request
from finance_a2a_api import render_markdown
port = os.environ.get("TEMPORAL_DEV_UI_PORT", "8233")
with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/nexus/endpoints", timeout=10) as r:
    eps = json.load(r)["endpoints"]
ep = next(e for e in eps if e["spec"]["name"] == "finance-agent")
desc = json.loads(base64.b64decode(ep["spec"]["description"]["data"]))
assert desc == render_markdown(), "Web UI API description != FINANCE_AGENT_CARD markdown"
print(f"Web UI :{port} /api/v1/nexus/endpoints serves the Agent Card for finance-agent")
EOF

step "7. Q16: Standalone Nexus Operations from the CLI (no caller Workflow)"
temporal nexus operation execute --namespace agents --endpoint finance-agent \
  --service FinanceAgentA2A --operation get_agent_card \
  --operation-id "card-smoke-$(date +%s)" --input '{}' -o json >"$LOGS/card.json"
jq -r '"get_agent_card -> \(.result.name) v\(.result.version) (A2A \(.result.protocol_version)) skills=\([.result.skills[].id])"' "$LOGS/card.json"
ctx="smoke-cli-$(date +%s)"
temporal nexus operation execute --namespace agents --endpoint finance-agent \
  --service FinanceAgentA2A --operation send_message --operation-id "refund-$ctx" \
  --input "{\"role\":\"user\",\"message_id\":\"m1\",\"context_id\":\"$ctx\",\"parts\":[{\"kind\":\"text\",\"text\":\"Broken headphones\"},{\"kind\":\"data\",\"data\":{\"order_id\":\"1042\",\"amount_usd\":40,\"reason\":\"damaged\"}}]}" \
  -o json >"$LOGS/send.json"
jq -r '"send_message (runbook 7b.9 JSON) -> \(.result.id): \(.result.status.state): \(.result.status.message.parts[0].text)"' "$LOGS/send.json"
[[ "$(jq -r .result.status.state "$LOGS/send.json")" == "completed" ]]

step "8. smoke-1042: A2ASmokeWorkflow in agents -> finance-agent -> a2a-task-smoke-1042 in agent-services"
temporal workflow terminate --namespace agents --workflow-id smoke-1042 --reason "verify reset" >/dev/null 2>&1 || true
temporal workflow start --namespace agents --task-queue nexus-smoke-tq --type A2ASmokeWorkflow \
  --workflow-id smoke-1042 --id-reuse-policy AllowDuplicate --input '40' >/dev/null
temporal workflow result --namespace agents --workflow-id smoke-1042 -o json >"$LOGS/smoke-1042.json"
jq -r '.result | "result: \(.id) \(.status.state): \(.status.message.parts[0].text)"' "$LOGS/smoke-1042.json"
"${UV[@]}" python scripts/a2a_timeline.py smoke-1042 | tee "$LOGS/timeline-1042.txt"
temporal workflow show --namespace agents --workflow-id smoke-1042 -o json \
  | jq -r '[.events[].eventType | select(startswith("EVENT_TYPE_NEXUS"))] | "caller Nexus events: \(join(" "))"' \
  | sed 's/EVENT_TYPE_NEXUS_OPERATION_//g' | tee "$LOGS/nexus-events.txt"
grep -q "SCHEDULED COMPLETED SCHEDULED STARTED COMPLETED" "$LOGS/nexus-events.txt"
grep -q "linked:  agent-services/a2a-task-smoke-1042" "$LOGS/timeline-1042.txt"
grep -q "get_task: state=completed" "$LOGS/timeline-1042.txt"
grep -q "artifact refund_confirmation" "$LOGS/timeline-1042.txt"
echo "smoke-1042: Nexus Scheduled/Started/Completed, linked to a2a-task-smoke-1042 (completed, refund_confirmation)"

step "9. make task-state WF=smoke-1042 (get_task query in agent-services)"
temporal workflow query --namespace agent-services --workflow-id a2a-task-smoke-1042 --type get_task -o json \
  | jq -c '.queryResult[0] | {id, state: .status.state, artifacts: [.artifacts[].name]}'

step "10. smoke-2077 (\$450): over the auto-approval limit -> rejected (HITL arrives in v0.6)"
temporal workflow terminate --namespace agents --workflow-id smoke-2077 --reason "verify reset" >/dev/null 2>&1 || true
temporal workflow start --namespace agents --task-queue nexus-smoke-tq --type A2ASmokeWorkflow \
  --workflow-id smoke-2077 --id-reuse-policy AllowDuplicate --input '450' --input '"2077"' >/dev/null
temporal workflow result --namespace agents --workflow-id smoke-2077 -o json \
  | jq -r '.result | "result: \(.id) \(.status.state): \(.status.message.parts[0].text | .[0:160])…"' | tee "$LOGS/smoke-2077.txt"
grep -q "rejected" "$LOGS/smoke-2077.txt"

step "11. idempotency: one A2A context sent twice at once -> one Finance Workflow"
ctx="dup-$(date +%s)"
msg="{\"role\":\"user\",\"message_id\":\"m1\",\"context_id\":\"$ctx\",\"parts\":[{\"kind\":\"text\",\"text\":\"dup\"},{\"kind\":\"data\",\"data\":{\"order_id\":\"1042\",\"amount_usd\":40,\"reason\":\"damaged\"}}]}"
starts=()
for n in 1 2; do
  temporal nexus operation start --namespace agents --endpoint finance-agent --service FinanceAgentA2A \
    --operation send_message --operation-id "dup-$n-$ctx" --input "$msg" >/dev/null & starts+=("$!")
done
wait "${starts[@]}"  # not a bare `wait`: that would also wait for the Workers
for n in 1 2; do
  temporal nexus operation result --namespace agents --operation-id "dup-$n-$ctx" -o json \
    | jq -r --arg n "$n" '"operation dup-\($n): \(.result.id) \(.result.status.state)"'
done
runs=$(temporal workflow list --namespace agent-services --query "WorkflowId='a2a-task-$ctx'" -o json | jq length)
echo "Finance Workflow runs for context $ctx: $runs"
[[ "$runs" == "1" ]]

step "12. idempotency: issue_refund's payments call twice with one key -> one refund"
key="verify-key-$(date +%s)"
for n in 1 2; do
  curl -s -X POST "$PAYMENTS/refunds" -H "Idempotency-Key: $key" -H 'Content-Type: application/json' \
    -d '{"order_id":"1042","amount_usd":40.0}' | jq -c '{refund_id, replayed}'
done
count=$(curl -s "$PAYMENTS/refunds" | jq --arg k "$key" '[.[] | select(.idempotency_key==$k)] | length')
echo "refunds with key $key: $count"
[[ "$count" == "1" ]]
echo "all refunds issued in this run:"
curl -s "$PAYMENTS/refunds" | jq -r '.[] | "  \(.refund_id) order \(.order_id) $\(.amount_usd) key=\(.idempotency_key)"'

if [[ "$RECORD" == "1" ]]; then
  step "13. record replay fixtures"
  "${UV[@]}" python scripts/record_history.py smoke-1042 --out services/support-agent/tests/histories/smoke
  "${UV[@]}" python scripts/record_history.py a2a-task-smoke-1042 --namespace agent-services \
    --out services/finance-agent/tests/histories
fi

printf '\nVERIFY OK\n'
