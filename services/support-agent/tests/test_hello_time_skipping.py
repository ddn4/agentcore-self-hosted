"""HelloWorkflow on the time-skipping test server (the `workflow` layer, plan section 5)."""

import uuid

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from support_agent.workflows import HelloWorkflow

pytestmark = pytest.mark.workflow


@activity.defn(name="say_hello")
async def say_hello_mocked(name: str) -> str:
    return f"mocked hello {name}"


async def test_hello_workflow_with_mocked_activity(time_skipping_env: WorkflowEnvironment) -> None:
    client = time_skipping_env.client
    task_queue = f"test-{uuid.uuid4()}"
    async with Worker(
        client, task_queue=task_queue, workflows=[HelloWorkflow], activities=[say_hello_mocked]
    ):
        result = await client.execute_workflow(
            HelloWorkflow.run, "Dana", id=f"hello-{uuid.uuid4()}", task_queue=task_queue
        )
    assert result == "mocked hello Dana"
