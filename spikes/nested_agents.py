"""Spike (v0.3, Q20): nested TemporalAgents as tools inside one Workflow.

    uv run python spikes/nested_agents.py            # offline (make spike-nested): no AWS
    RUN_BEDROCK_TESTS=1 uv run python spikes/nested_agents.py --live
                                                     # + Cases A and B on Opus 5.5

Runbook 5.6 asks: is calling a nested ``TemporalAgent.invoke_async`` from an
in-workflow ``@tool`` supported, and does each sub-agent's model call show up as
its own ``invoke_model`` Activity? Runs on the time-skipping test server with
the services' real Worker (``support_agent.worker_factory``):

1. The service shape (hand-written ``@tool``, fresh sub-agent per call): Cases
   A (1042) and B (2077). Prints the attributed Activity sequence and replays
   the history.
2. Strands' own ``Agent.as_tool()`` (``demo_testing.probes``): counts the
   ``TimerStarted`` events its cancel-signal watcher writes (it polls with
   ``asyncio.sleep(0.05)``, a durable timer in a Workflow).
3. One shared sub-agent instance (runbook 5.6's sketch) called twice in one
   turn: Strands' per-agent lock refuses the second call.

``--live`` repeats step 1 with the Worker's pinned Opus 5.5 plugin
(``config.build_strands_plugin()``): about 12 small model calls in total.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor

from _wire import aws_session_ok
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client
from temporalio.strands_agents import StrandsPlugin
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from demo_testing.fixtures import start_two_namespace_env
from demo_testing.history import attributed_activities, count_events
from demo_testing.probes import (
    PROBE_SUB_AGENT_PROMPT,
    NativeAsToolProbeWorkflow,
    SharedSubAgentProbeWorkflow,
)
from demo_testing.scripted_model import (
    ModelCall,
    Responder,
    ScriptedModel,
    ToolCall,
    Turn,
    client_with_plugin,
    scripted_plugin,
)
from demo_testing.support_script import SupportPrompts, support_responder
from support_agent import agents, config
from support_agent.worker_factory import WORKFLOWS, build_worker
from support_agent.workflows import SupportSupervisorWorkflow

TICKETS = {
    "A": "Order #1042, my headphones arrived broken. I want my money back.",
    "B": "Order #2077: my laptop arrived with a cracked screen. I want a refund.",
}
PROMPTS = SupportPrompts(
    agents.SUPERVISOR_PROMPT, agents.ORDER_LOOKUP_PROMPT, agents.RETURN_POLICY_PROMPT
)


async def run_case(env: WorkflowEnvironment, plugin: StrandsPlugin, case: str) -> bool:
    client = client_with_plugin(env.client, plugin)
    task_queue = f"spike-nested-{uuid.uuid4()}"
    with ThreadPoolExecutor(max_workers=4) as executor:
        async with build_worker(
            client, task_queue=task_queue, activity_executor=executor, max_cached_workflows=0
        ):
            handle = await client.start_workflow(
                SupportSupervisorWorkflow.run, TICKETS[case], id=task_queue, task_queue=task_queue
            )
            reply = await handle.result()
            history = await handle.fetch_history()
            runs = await handle.query(SupportSupervisorWorkflow.sub_agent_runs)
    print(f"\n--- Case {case}: {TICKETS[case]}")
    for n, line in enumerate(attributed_activities(history), 1):
        print(f"  {n:>2}. {line}")
    for run in runs:
        print(f"  {run['agent']}: {run['answer']}")
    print(f"  reply: {reply}")
    summaries = set(attributed_activities(history))
    attributed = {
        f"invoke_model [{name}]"
        for name in (agents.SUPERVISOR_NAME, agents.ORDER_LOOKUP_NAME, agents.RETURN_POLICY_NAME)
    } <= summaries
    replayer = Replayer(workflows=WORKFLOWS, plugins=[scripted_plugin(ScriptedModel([]))])
    replay = await replayer.replay_workflow(history, raise_on_replay_failure=False)
    print(f"  three agents attributed: {attributed}; replay: {replay.replay_failure or 'ok'}")
    escalated_ok = case != "B" or "escalat" in reply.lower()
    print(f"  escalation mentioned (Case B only): {escalated_ok}")
    return attributed and replay.replay_failure is None and escalated_ok


def _probe_responder(parallel: bool) -> Responder:
    def respond(call: ModelCall) -> Turn:
        if call.system_prompt == PROBE_SUB_AGENT_PROMPT:
            return f"sub-agent answer to {call.last_user_text!r}"
        if call.tool_results:
            return " | ".join(call.tool_results)
        if parallel:
            return [
                ToolCall("sub_agent", {"question": "a"}),
                ToolCall("sub_agent", {"question": "b"}),
            ]
        return ToolCall("sub_agent", {"input": "a"})

    return respond


async def run_probe(env: WorkflowEnvironment, workflow: type, parallel: bool) -> tuple[str, int]:
    # 1 s of simulated model latency per turn, as a slow real model would have.
    # Real-time server: on the time-skipping one, step 2's timers advance
    # virtual time while the model Activity runs (it can then time out).
    model = ScriptedModel(_probe_responder(parallel), delay=1.0)
    client: Client = client_with_plugin(env.client, scripted_plugin(model))
    task_queue = f"spike-probe-{uuid.uuid4()}"
    async with Worker(client, task_queue=task_queue, workflows=[workflow]):
        handle = await client.start_workflow(
            workflow.run, "go", id=task_queue, task_queue=task_queue
        )
        result = await handle.result()
        history = await handle.fetch_history()
    return result, count_events(history, EventType.EVENT_TYPE_TIMER_STARTED)


async def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--live", action="store_true", help="also run Cases A/B on Opus 5.5")
    args = parser.parse_args()
    if "a2a_mode" in inspect.signature(SupportSupervisorWorkflow.run).parameters:
        # From v0.5 the Supervisor always calls Finance over Nexus, and this spike's
        # server has no `finance-agent` endpoint: the first Workflow task would fail
        # forever. Q20 is answered (ledger 10); live sub-agent runs now come from
        # scripts/verify/v0.5-a2a-over-nexus.sh and v0.6 without VERIFY_SKIP_BEDROCK.
        print("nested_agents: v0.3 spike; run it from a v0.3-sub-agents checkout")
        return 2
    ok = True
    env = await start_two_namespace_env()  # real time: see the note in run_probe
    try:
        print("=== 1. service shape: hand-written @tool, fresh sub-agent per call (scripted)")
        for case in TICKETS:
            ok &= await run_case(
                env, scripted_plugin(ScriptedModel(support_responder(PROMPTS))), case
            )

        print("\n=== 2. Strands Agent.as_tool() inside a Workflow (scripted, 1 s per model turn)")
        result, timers = await run_probe(env, NativeAsToolProbeWorkflow, parallel=False)
        print(
            f"  result: {result}\n  TimerStarted events: {timers} (the hand-written tool writes 0)"
        )
        ok &= timers > 0

        print("\n=== 3. one shared sub-agent instance, called twice in one turn (scripted)")
        result, _ = await run_probe(env, SharedSubAgentProbeWorkflow, parallel=True)
        print(f"  tool results: {result}")
        ok &= "ConcurrencyException" in result

        if args.live:
            if os.environ.get("RUN_BEDROCK_TESTS") != "1":
                print("--live needs RUN_BEDROCK_TESTS=1 (calls Bedrock, costs tokens)")
                return 2
            if not aws_session_ok(config.aws_region()):
                return 2
            print(f"\n=== 4. live: Cases A and B on {config.model_id()}")
            for case in TICKETS:
                ok &= await run_case(env, config.build_strands_plugin(), case)
    finally:
        await env.shutdown()
    print(f"\nnested_agents: {'OK' if ok else 'FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
