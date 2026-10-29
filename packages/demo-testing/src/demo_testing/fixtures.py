"""Pytest fixtures: one two-namespace Temporal dev server per test session.

Loaded as a pytest plugin from the root ``conftest.py``. The pattern follows the
samples-python ``tests/conftest.py`` session ``env`` fixture and the Python SDK
testing guide (``WorkflowEnvironment.start_local`` shared across tests,
``start_time_skipping`` only where a test needs it, because it cannot be
shared).

The dev server is the locally installed Temporal CLI (pinned to 1.9.1, which
embeds server 1.32.0) rather than a version the SDK downloads, so the tests run
against the same binary as ``make dev-server``. If no ``temporal`` is on PATH,
the SDK downloads the same pinned release.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import AsyncIterator
from typing import Any

import pytest_asyncio
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment

from demo_testing import AGENT_SERVICES_NAMESPACE, AGENTS_NAMESPACE, TEMPORAL_CLI_VERSION


def temporal_cli_version(path: str) -> str:
    """Return the CLI version reported by ``temporal --version`` (e.g. ``1.9.1``)."""
    out = subprocess.run([path, "--version"], check=True, capture_output=True, text=True).stdout
    match = re.search(r"temporal version (\S+)", out)
    if not match:
        raise RuntimeError(f"cannot parse `temporal --version` output: {out!r}")
    return match.group(1)


def dev_server_kwargs() -> dict[str, Any]:
    """Arguments that pin the dev server binary used by ``start_local``."""
    existing = shutil.which("temporal")
    if existing:
        return {"dev_server_existing_path": existing}
    # Fallback: the SDK downloads the CLI release; the version string is the
    # GitHub release tag ("v1.9.1"), as in samples-python's tests/conftest.py.
    return {"dev_server_download_version": f"v{TEMPORAL_CLI_VERSION}"}


async def start_two_namespace_env() -> WorkflowEnvironment:
    """Start a dev server with namespaces ``agents`` (the client's) and ``agent-services``."""
    return await WorkflowEnvironment.start_local(
        namespace=AGENTS_NAMESPACE,
        dev_server_extra_args=["--namespace", AGENT_SERVICES_NAMESPACE],
        **dev_server_kwargs(),
    )


@pytest_asyncio.fixture(scope="session")
async def two_namespace_env() -> AsyncIterator[WorkflowEnvironment]:
    """Session-wide dev server. Its client is bound to namespace ``agents``."""
    env = await start_two_namespace_env()
    try:
        yield env
    finally:
        await env.shutdown()


@pytest_asyncio.fixture(scope="session")
async def agents_client(two_namespace_env: WorkflowEnvironment) -> Client:
    """Client for the caller namespace ``agents`` (team Support)."""
    return two_namespace_env.client


@pytest_asyncio.fixture(scope="session")
async def agent_services_client(two_namespace_env: WorkflowEnvironment) -> Client:
    """Client for the handler namespace ``agent-services`` (team Finance)."""
    return await two_namespace_env.connect_client(namespace=AGENT_SERVICES_NAMESPACE)


@pytest_asyncio.fixture
async def time_skipping_env() -> AsyncIterator[WorkflowEnvironment]:
    """Per-test time-skipping environment (cannot be shared between tests)."""
    env = await WorkflowEnvironment.start_time_skipping()
    try:
        yield env
    finally:
        await env.shutdown()
