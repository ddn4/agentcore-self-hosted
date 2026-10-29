"""Spike (v0.4): does the A2A hop over Nexus run under the time-skipping test server?

    uv run python spikes/nexus_time_skipping.py        (no AWS; a few seconds)

Plan v0.4 verify item "Nexus under time-skipping. Expected: no, so use
start_local." Tries the real hop (``A2ASmokeWorkflow`` -> endpoint
``finance-agent`` -> Finance Worker with a scripted model) on
``WorkflowEnvironment.start_time_skipping()`` (the Java test server; one
namespace, so caller and handler share it), with a short timeout, and prints
what happened. samples-python skips its Nexus tests on that server
("Nexus tests don't work under the Java test server").

Then runs the same hop on ``start_time_skipping_v2()`` (temporalio >= 1.35,
experimental: a real dev server with per-Workflow virtual clocks), which
matters for v0.6's 24 h approval timeout.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

from temporalio.testing import WorkflowEnvironment

from demo_testing.finance_script import finance_responder
from demo_testing.fixtures import dev_server_kwargs
from demo_testing.scripted_model import ScriptedModel, client_with_plugin, scripted_plugin
from finance_a2a_api import ENDPOINT
from finance_agent import agents
from finance_agent.worker import build_worker
from payments_mock.server import serve_in_thread
from support_agent.smoke import A2ASmokeWorkflow
from support_agent.smoke_worker import build_smoke_worker

TIMEOUT = 20


async def try_hop(label: str, env: WorkflowEnvironment) -> None:
    finance_tq, smoke_tq = f"fin-{uuid.uuid4().hex[:6]}", f"smoke-{uuid.uuid4().hex[:6]}"
    try:
        endpoint = await env.create_nexus_endpoint(ENDPOINT, finance_tq)
    except Exception as err:
        print(f"{label}: create_nexus_endpoint failed: {type(err).__name__}: {err}")
        return
    finance = client_with_plugin(
        env.client, scripted_plugin(ScriptedModel(finance_responder(agents.FINANCE_PROMPT)))
    )
    try:
        async with (
            build_worker(finance, task_queue=finance_tq),
            build_smoke_worker(env.client, task_queue=smoke_tq),
        ):
            task = await asyncio.wait_for(
                env.client.execute_workflow(
                    A2ASmokeWorkflow.run,
                    args=[40.0, "1042"],
                    id=f"smoke-ts-{uuid.uuid4().hex[:6]}",
                    task_queue=smoke_tq,
                    execution_timeout=timedelta(seconds=TIMEOUT),
                ),
                TIMEOUT + 5,
            )
        print(f"{label}: hop WORKS: task {task.id} is {task.status.state}")
    except Exception as err:
        print(f"{label}: hop FAILS: {type(err).__name__}: {err} (cause: {err.__cause__!r})")
    finally:
        try:
            await env.delete_nexus_endpoint(endpoint)
        except Exception:
            pass


async def main() -> None:
    server, url = serve_in_thread()
    import os

    os.environ["PAYMENTS_URL"] = url
    try:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            await try_hop("time-skipping v1 (Java test server)", env)
        async with await WorkflowEnvironment.start_time_skipping_v2(**dev_server_kwargs()) as env:
            await try_hop("time-skipping v2 (dev server, per-Workflow clocks)", env)
    finally:
        server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
