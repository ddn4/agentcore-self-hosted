"""Check one finished Support conversation against the v0.5 acceptance (verify script helper).

    uv run python scripts/check_a2a_run.py support-1042 completed [--mode tool|handoff]

Caller history (namespace ``agents``): ``get_agent_card`` first, then the
sub-agents' ``invoke_model`` turns, then exactly one ``send_message`` after the
Return Policy agent's decision, then the Supervisor's last turn; the
``send_message`` operation links to ``a2a-task-<id>`` in ``agent-services``.
Finance side: that task ended in the expected A2A state and is not a child of
the caller. Reply: quotes the refund ID when a refund was issued. Exits 1 on
the first failed check. Honours TEMPORAL_PROFILE / TEMPORAL_CONFIG_FILE.

v0.6: ``rejected`` replies must apologise, ``failed`` replies must apologise and
escalate; ``--approver NAME`` checks the human named on the refund confirmation.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys

from temporalio.client import Client

from demo_testing.history import nexus_operations, timeline
from finance_a2a_api import A2ATask, first_data
from support_agent import config

CARD = "nexus FinanceAgentA2A.get_agent_card"
SEND = "nexus FinanceAgentA2A.send_message"
SUPERVISOR = "invoke_model [Support Supervisor]"


def check(ok: bool, what: str) -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        sys.exit(1)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Check a v0.5 Support run (verify helper)")
    parser.add_argument("workflow_id")
    parser.add_argument("state", help="expected A2A task state (completed, rejected, ...)")
    parser.add_argument("--mode", choices=["tool", "handoff"], default="tool")
    parser.add_argument("--approver", help="v0.6: expected approved_by on the refund")
    args = parser.parse_args()

    caller = await config.connect(plugins=[config.build_strands_plugin()])
    handle = caller.get_workflow_handle(args.workflow_id)
    history = await handle.fetch_history()
    steps = timeline(history)
    print(
        f"{args.workflow_id}: {len(steps)} steps: "
        + " → ".join(s.split(" [")[-1].rstrip("]") for s in steps)
    )
    check(steps[0] == CARD, "first step is A2A discovery: get_agent_card")
    check(steps.count(SEND) == 1, "exactly one send_message (Finance asked once)")
    send = steps.index(SEND)
    policy = max(i for i, s in enumerate(steps) if s == "invoke_model [Return Policy agent]")
    order = min(i for i, s in enumerate(steps) if s == "invoke_model [Order Lookup agent]")
    check(0 < order < policy < send, "sub-agents' invoke_model turns come before send_message")
    check(steps[send - 1] == SUPERVISOR and steps[-1] == SUPERVISOR, "Supervisor turns around it")
    (op,) = [o for o in nexus_operations(history) if o.operation == "send_message"]
    task_id = f"a2a-task-{args.workflow_id}"
    check(
        op.links == (f"agent-services/{task_id}",),
        f"send_message links to agent-services/{task_id}",
    )
    via = "ask_finance_agent tool" if args.mode == "tool" else "handoff"
    check(op.summary.endswith(f"via {via}"), f"summary: {op.summary}")

    a2a = await handle.query("a2a")
    check(a2a["mode"] == args.mode, f"a2a query: mode={a2a['mode']}")
    (exchange,) = a2a["exchanges"]
    check(exchange["state"] == args.state, f"a2a exchange state={exchange['state']}")
    print(f"  ..   guard re-prompts: {a2a['guard_reprompts']}")

    handler = await Client.connect(**{**config.connect_config(), "namespace": "agent-services"})
    fin = handler.get_workflow_handle(task_id)
    desc = await fin.describe()
    check(desc.parent_id is None, f"{task_id} has no parent Workflow (not a child)")
    task = await fin.query("get_task", result_type=A2ATask)
    check(task.status.state == args.state, f"Finance get_task: state={task.status.state}")

    reply = str(await handle.result())
    if args.state == "completed":
        refund = exchange["refund_id"]
        check(bool(refund) and refund in reply, f"reply quotes Finance's refund ID {refund}")
    sorry = re.search(r"sorry|apolog", reply, re.IGNORECASE) is not None
    if args.state == "rejected":
        check(sorry, "reply apologises (Finance rejected the refund)")
    if args.state == "failed":
        escalated = re.search(r"escalat", reply, re.IGNORECASE) is not None
        check(sorry and escalated, "reply apologises and escalates (Finance could not decide)")
    if args.approver:
        data = first_data(task.artifacts[0].parts) if task.artifacts else None
        approved_by = (data or {}).get("approved_by")
        check(approved_by == args.approver, f"refund_confirmation approved_by={approved_by}")
    print("  ok   all checks passed")


if __name__ == "__main__":
    asyncio.run(main())
