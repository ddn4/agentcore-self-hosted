"""HelloWorkflow on the shared two-namespace dev server (namespace agents)."""

import uuid

import pytest
from temporalio.client import Client
from temporalio.worker import Worker

from support_agent.activities import say_hello
from support_agent.workflows import HelloWorkflow

pytestmark = pytest.mark.integration


async def test_hello_workflow_in_agents_namespace(agents_client: Client) -> None:
    task_queue = f"test-{uuid.uuid4()}"
    async with Worker(
        agents_client, task_queue=task_queue, workflows=[HelloWorkflow], activities=[say_hello]
    ):
        result = await agents_client.execute_workflow(
            HelloWorkflow.run, "Dana", id=f"hello-{uuid.uuid4()}", task_queue=task_queue
        )
    assert agents_client.namespace == "agents"
    assert result == "Hello, Dana, from namespace agents!"
