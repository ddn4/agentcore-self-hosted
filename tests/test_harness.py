"""Fixture smoke test: the harness itself (plan section 3.1, v0.1 acceptance)."""

import re
import shutil
from pathlib import Path

import pytest
from temporalio.api.workflowservice.v1 import GetSystemInfoRequest, ListNamespacesRequest
from temporalio.client import Client

from demo_testing import AGENT_SERVICES_NAMESPACE, AGENTS_NAMESPACE, TEMPORAL_CLI_VERSION
from demo_testing.fixtures import temporal_cli_version

REPO_ROOT = Path(__file__).resolve().parents[1]


def pinned_cli_version() -> str:
    text = (REPO_ROOT / "docs" / "versions.md").read_text()
    match = re.search(r"^\|\s*Temporal CLI\s*\|\s*([0-9.]+)\s*\|", text, re.MULTILINE)
    assert match, "docs/versions.md has no '| Temporal CLI | x.y.z |' row"
    return match.group(1)


@pytest.mark.unit
def test_pins_agree() -> None:
    assert pinned_cli_version() == TEMPORAL_CLI_VERSION


@pytest.mark.integration
def test_installed_cli_matches_pin() -> None:
    path = shutil.which("temporal")
    if path is None:
        pytest.skip("no temporal on PATH: the SDK downloads the pinned release instead")
    assert temporal_cli_version(path) == pinned_cli_version()


@pytest.mark.integration
async def test_dev_server_has_both_namespaces(
    agents_client: Client, agent_services_client: Client
) -> None:
    resp = await agents_client.workflow_service.list_namespaces(ListNamespacesRequest())
    names = {ns.namespace_info.name for ns in resp.namespaces}
    assert {AGENTS_NAMESPACE, AGENT_SERVICES_NAMESPACE} <= names
    assert agents_client.namespace == AGENTS_NAMESPACE
    assert agent_services_client.namespace == AGENT_SERVICES_NAMESPACE


@pytest.mark.integration
async def test_dev_server_is_server_1_32(agents_client: Client) -> None:
    info = await agents_client.workflow_service.get_system_info(GetSystemInfoRequest())
    assert info.server_version.startswith("1.32."), info.server_version
