"""The Worker exactly as worker.py builds it: pinned StrandsPlugin on the client.

Proves offline (no model call) that the plugin's pydantic data converter and
sandbox passthroughs work with this service's Workflows. Idiom follows
samples-python tests/strands_plugin/hello_world_test.py (plugin appended to the
test client via client.config()).
"""

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from temporalio.client import Client
from temporalio.contrib.pydantic import PydanticPayloadConverter

from support_agent import config
from support_agent.worker_factory import build_worker
from support_agent.workflows import HelloWorkflow

pytestmark = pytest.mark.integration


async def test_hello_workflow_on_plugin_worker(agents_client: Client) -> None:
    task_queue = f"test-{uuid.uuid4()}"
    client_config = agents_client.config()
    client_config["plugins"] = [*client_config["plugins"], config.build_strands_plugin()]
    client = Client(**client_config)
    assert client.data_converter.payload_converter_class is PydanticPayloadConverter

    with ThreadPoolExecutor(max_workers=2) as executor:
        async with build_worker(client, task_queue=task_queue, activity_executor=executor):
            result = await client.execute_workflow(
                HelloWorkflow.run, "Dana", id=f"hello-plugin-{uuid.uuid4()}", task_queue=task_queue
            )
    assert result == "Hello, Dana, from namespace agents!"
