"""Fixtures for the demo console's unit tests; the fakes are in ``demo_console.fakes``."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory

from demo_console.fakes import FakeGateway, FakeHttp, FakeRunner, load_history

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def load() -> Callable[..., WorkflowHistory]:
    """``load("case-a.support", "support-1042")``: a recorded history (scrubbed)."""

    def _load(name: str, workflow_id: str = "wf") -> WorkflowHistory:
        return load_history(FIXTURES / f"{name}.json", workflow_id)

    return _load


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway()


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def http() -> FakeHttp:
    return FakeHttp()
