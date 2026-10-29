"""HelloWorkflow on the shared two-namespace dev server (namespace agent-services)."""

import uuid

import pytest
from temporalio.client import Client
from temporalio.worker import Worker

from finance_agent.activities import say_hello
from finance_agent.workflows import HelloWorkflow

pytestmark = pytest.mark.integration


async def test_hello_workflow_in_agent_services_namespace(agent_services_client: Client) -> None:
    task_queue = f"test-{uuid.uuid4()}"
    async with Worker(
        agent_services_client,
        task_queue=task_queue,
        workflows=[HelloWorkflow],
        activities=[say_hello],
    ):
        result = await agent_services_client.execute_workflow(
            HelloWorkflow.run, "Dana", id=f"hello-{uuid.uuid4()}", task_queue=task_queue
        )
    assert agent_services_client.namespace == "agent-services"
    assert result == "Hello, Dana, from namespace agent-services!"
