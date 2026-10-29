# Finance Refund Agent (A2A) · v1.0.0

**Owner:** Finance · **Runs on:** EKS (`agent-services`) · **A2A-shaped:** protocol 0.3.0

**Service:** `FinanceAgentA2A` — `get_agent_card` (sync), `send_message` (A2A task, Workflow `a2a-task-<context_id>`)

Owned by Finance. Assesses refund requests (fraud risk, refund history, approval limits) and issues refunds through the payments system. Refunds above $100, or at HIGH fraud risk, need a human Finance approver.

**Skill `refund` (Refund request):** Approve and issue a customer refund. Send a text part (why) and a data part {order_id, amount_usd, reason}. The task completes with a refund_confirmation artifact {refund_id, amount_usd, approved_by}.
Tags: refund, payments, finance

Task states: `submitted` → `working` → `completed` | `rejected` | `failed` (`input-required` while a Finance approver decides).
