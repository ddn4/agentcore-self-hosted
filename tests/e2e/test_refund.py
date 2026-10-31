"""End to end against a RUNNING stack: Cases A and B through the real Workers (``make e2e``).

Profile-driven (plan §2.2, §5 "End to end"): the connection comes from envconfig
(``TEMPORAL_CONFIG_FILE`` + ``TEMPORAL_PROFILE``: ``local`` now, ``kind`` from
v0.9, ``eks-pf`` from v0.11), exactly as the Workers connect; the Support task
queue from ``SUPPORT_TASK_QUEUE`` (``.env``; default per profile below) and the
A2A mode from ``SUPPORT_A2A_MODE``. Nothing is started here: the Support Worker,
the Finance Worker, payments-mock and the ``finance-agent`` endpoint must be up
(``make dev``, or the scripted Workers offline). Skipped unless ``RUN_E2E=1``.

v0.6: Case B is the human-in-the-loop path. The test plays the Finance approver:
it waits until the A2A task is ``input-required`` (``get_task``), then signals
``approve`` (or ``reject``) as ``e2e.approver``, exactly like ``make approve``.
The Support starter passes ``SUPPORT_NEXUS_SEND_TIMEOUT`` (default 25h) as the
third Workflow input, as ``scripts/demo/case-a.sh`` does.

With the real model this costs one Case A and two Case Bs (about 8 Support and 2
Finance model calls each).

v0.13, ``TEMPORAL_PROFILE=eks-pf``: Support runs only as AgentCore sessions started by the
Worker Controller (WCI) on ``support-tq``. Before each Case the test waits until every recent
session has drained (nothing polls), and in Case B it approves/rejects only after the
conversation's session has drained (``demo_testing.agentcore_logs``). It then asserts the
**WCI invocation counts**: AgentCore sessions that ran the Workflow (CloudWatch log streams
naming it) and ``InvokeWorker`` Activities in the WCI history during the Case: Case A = 1,
Case B = 2 (the second one caused by the Nexus completion, Q15). Needs AWS credentials.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from temporalio.client import Client, WorkflowExecutionStatus

from demo_testing.a2a_tasks import wait_for_a2a_task
from demo_testing.history import nexus_operations, timeline
from finance_a2a_api import A2ATask
from support_agent import config
from support_agent.workflows import SupportSupervisorWorkflow

pytestmark = pytest.mark.e2e

#: Support's queue per profile when SUPPORT_TASK_QUEUE is unset (plan §3.3).
DEFAULT_QUEUE = {"local": "support-dev-tq"}
CASE_A = "Order #1042, my headphones arrived broken. I want my money back."
CASE_B = "Order #2077: my laptop arrived with a cracked screen. I want a refund."
TIMEOUT = timedelta(minutes=5)


def profile() -> str:
    return os.environ.get("TEMPORAL_PROFILE", "local")


def task_queue() -> str:
    return os.environ.get("SUPPORT_TASK_QUEUE") or DEFAULT_QUEUE.get(profile(), "support-tq")


def cloud() -> bool:
    """Support on AgentCore (WCI-invoked sessions): count sessions and invocations."""
    return profile() == "eks-pf"


def wci_invocations(build: str, since: float, until: datetime) -> int:
    """``InvokeWorker`` Activities of the version's WCI between ``since`` (epoch s) and ``until``.

    Bounded by the Workflow's close time: a query on the closed Workflow (``a2a`` below,
    ``make timeline``) is a task on ``support-tq`` too and may make the WCI invoke once more
    (v0.13 finding), which is not part of the Case.
    """
    iso = datetime.fromtimestamp(since, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = subprocess.run(
        ["scripts/wci-history.sh", build, "--since", iso],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    stamp = until.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")
    return sum(
        1
        for line in out.splitlines()
        if line.rstrip().endswith("activity  InvokeWorker") and line.strip()[:26] <= stamp
    )


async def current_build(client: Client) -> str:
    out = await asyncio.to_thread(
        subprocess.run,
        [
            "temporal",
            "worker",
            "deployment",
            "describe",
            "--namespace",
            "agents",
            "--name",
            "support-supervisor",
            "-o",
            "json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return str(json.loads(out.stdout)["routingConfig"]["currentVersionBuildID"])


async def finance_client() -> Client:
    return await Client.connect(**{**config.connect_config(), "namespace": "agent-services"})


#: Per-Case AgentCore evidence (eks-pf only): workflow id -> (sessions, InvokeWorker count)
CLOUD_COUNTS: dict[str, tuple[int, int]] = {}


async def run_case(
    ticket: str, verdict: str | None = None
) -> tuple[Client, str, str, dict[str, object]]:
    client = await config.connect(plugins=[config.build_strands_plugin()])
    mode = os.environ.get("SUPPORT_A2A_MODE", "tool")
    wf_id = f"e2e-{profile()}-{uuid.uuid4().hex[:8]}"
    started = time.time()
    if cloud():
        from demo_testing import agentcore_logs

        build = await current_build(client)
        # Nothing may still be polling from an earlier Case, or it would take this one.
        await asyncio.to_thread(agentcore_logs.wait_idle, int((started - 600) * 1000), 300, build)
        started = time.time()
    handle = await client.start_workflow(
        SupportSupervisorWorkflow.run,
        args=[ticket, mode, config.nexus_send_timeout()],
        id=wf_id,
        task_queue=task_queue(),
        execution_timeout=TIMEOUT,
    )
    if verdict is not None:  # play the Finance approver (make approve / make reject)
        finance = await finance_client()
        task = await wait_for_a2a_task(finance, f"a2a-task-{wf_id}", "input-required", within=180)
        if cloud():
            # Approve only once Support's compute is gone (debounce 30 s + log delivery).
            await asyncio.to_thread(
                agentcore_logs.wait_drained, wf_id, int((started - 5) * 1000), 300, build
            )
            await asyncio.sleep(10)
        await finance.get_workflow_handle(task.id).signal(verdict, {"approver": "e2e.approver"})
    reply = await asyncio.wait_for(handle.result(), TIMEOUT.total_seconds())
    closed = (await handle.describe()).close_time
    a2a = await handle.query(SupportSupervisorWorkflow.a2a)
    if cloud():
        sessions = await asyncio.to_thread(
            agentcore_logs.wait_drained, wf_id, int((started - 5) * 1000), 300, build
        )
        assert closed is not None
        invokes = await asyncio.to_thread(wci_invocations, build, started - 2, closed)
        CLOUD_COUNTS[wf_id] = (len(sessions), invokes)
    return client, wf_id, reply, a2a


def assert_cloud_counts(wf_id: str, expected: int) -> None:
    """eks-pf: the Case ran in ``expected`` AgentCore sessions, each one WCI invocation."""
    if not cloud():
        return
    sessions, invokes = CLOUD_COUNTS[wf_id]
    print(f"{wf_id}: AgentCore sessions {sessions}, WCI InvokeWorker {invokes}")
    assert sessions == expected
    assert invokes == expected


async def finance_task(wf_id: str) -> A2ATask:
    finance = await finance_client()
    handle = finance.get_workflow_handle(f"a2a-task-{wf_id}")
    desc = await handle.describe()
    assert desc.status == WorkflowExecutionStatus.COMPLETED
    return await handle.query("get_task", result_type=A2ATask)


async def test_case_a_refund_is_issued_by_finance() -> None:
    client, wf_id, reply, a2a = await run_case(CASE_A)
    exchanges = a2a["exchanges"]
    assert isinstance(exchanges, list) and len(exchanges) == 1
    refund_id = exchanges[0]["refund_id"]
    assert exchanges[0]["state"] == "completed" and refund_id and refund_id in reply
    history = await client.get_workflow_handle(wf_id).fetch_history()
    steps = timeline(history)
    assert steps[0] == "nexus FinanceAgentA2A.get_agent_card"
    assert steps.count("nexus FinanceAgentA2A.send_message") == 1
    (send,) = [o for o in nexus_operations(history) if o.operation == "send_message"]
    assert send.links == (f"agent-services/a2a-task-{wf_id}",)
    task = await finance_task(wf_id)
    assert task.status.state == "completed"
    assert_cloud_counts(wf_id, 1)


async def test_case_b_waits_for_the_approver_then_completes() -> None:
    """v0.6: $450 -> input-required -> approve -> the reply quotes Finance's refund."""
    _, wf_id, reply, a2a = await run_case(CASE_B, "approve")
    exchanges = a2a["exchanges"]
    assert isinstance(exchanges, list) and len(exchanges) == 1
    refund_id = exchanges[0]["refund_id"]
    assert exchanges[0]["state"] == "completed" and refund_id and refund_id in reply
    task = await finance_task(wf_id)
    assert task.status.state == "completed"
    assert "e2e.approver" in (task.status.message.parts[0].text or "")  # type: ignore[union-attr]
    assert_cloud_counts(wf_id, 2)  # v0.13: the Nexus completion caused the second invocation


async def test_case_b_rejected_by_the_approver() -> None:
    _, wf_id, reply, a2a = await run_case(CASE_B, "reject")
    exchanges = a2a["exchanges"]
    assert isinstance(exchanges, list) and len(exchanges) == 1
    assert exchanges[0]["state"] == "rejected" and exchanges[0]["refund_id"] is None
    assert "450" in reply
    task = await finance_task(wf_id)
    assert task.status.state == "rejected"
    assert_cloud_counts(wf_id, 2)
