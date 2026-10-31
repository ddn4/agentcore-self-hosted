#!/usr/bin/env bash
# issue_refund is idempotent on EKS (runbook §5 "End to end", last item; plan v0.13).
#   scripts/refund-idempotency.sh <support-workflow-id>     (make refund-idempotency WF=support-2077)
# Finance's issue_refund calls payments-mock with Idempotency-Key = the A2A task ID
# (a2a-task-<support workflow id>, finance_agent.workflows). This replays exactly that call
# from inside the payments-mock pod (its own loopback; nothing new is deployed): the same key
# and body must return the SAME refund with "replayed": true, and the ledger must still hold
# exactly one refund for the key. Read-only apart from that one idempotent replay.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
WF="${1:?support workflow id}"
KEY="a2a-task-$WF"
export KUBECONFIG="${KUBECONFIG:-$PWD/.temporal/kubeconfig-eks}"
kubectl --context agentcore-demo-eks -n temporal-workers exec -i deploy/payments-mock -- python - "$KEY" <<'EOF'
import json, sys, urllib.request
key, base = sys.argv[1], "http://127.0.0.1:8080"
mine = [r for r in json.load(urllib.request.urlopen(base + "/refunds")) if r["idempotency_key"] == key]
if len(mine) != 1:
    sys.exit(f"expected exactly one refund for {key}, found {len(mine)}")
first = mine[0]
print(f"before: {first['refund_id']} order {first['order_id']} ${first['amount_usd']:.2f} key={key}")
body = json.dumps({"order_id": first["order_id"], "amount_usd": first["amount_usd"]}).encode()
req = urllib.request.Request(base + "/refunds", data=body, method="POST",
                             headers={"Idempotency-Key": key, "Content-Type": "application/json"})
with urllib.request.urlopen(req) as resp:
    again = json.load(resp)
    print(f"replay: HTTP {resp.status} refund_id={again['refund_id']} replayed={again['replayed']}")
after = [r for r in json.load(urllib.request.urlopen(base + "/refunds")) if r["idempotency_key"] == key]
print(f"after: {len(after)} refund(s) with key {key}")
ok = again["refund_id"] == first["refund_id"] and again["replayed"] is True and len(after) == 1
print("idempotent: " + ("yes" if ok else "NO"))
sys.exit(0 if ok else 1)
EOF
