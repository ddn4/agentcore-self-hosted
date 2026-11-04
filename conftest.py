"""Root conftest: shared fixtures and marker gating for every test suite."""

import os

import pytest

pytest_plugins = ["demo_testing.fixtures"]


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Live-model and e2e tests never run by accident (make check needs no AWS)."""
    skip_bedrock = pytest.mark.skip(reason="live Bedrock: set RUN_BEDROCK_TESTS=1 (make test-live)")
    skip_e2e = pytest.mark.skip(reason="e2e: needs a running stack; make e2e (RUN_E2E=1)")
    for item in items:
        if "bedrock" in item.keywords and os.environ.get("RUN_BEDROCK_TESTS") != "1":
            item.add_marker(skip_bedrock)
        if "e2e" in item.keywords and os.environ.get("RUN_E2E") != "1":
            item.add_marker(skip_e2e)
