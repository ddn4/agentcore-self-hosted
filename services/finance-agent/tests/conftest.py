"""Fixtures for the Finance test suite."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest

from payments_mock.server import PaymentsServer, serve_in_thread


@pytest.fixture
def activity_executor() -> Iterator[ThreadPoolExecutor]:
    """Thread pool for the synchronous Finance Activities."""
    with ThreadPoolExecutor(max_workers=4) as executor:
        yield executor


@pytest.fixture
def payments(monkeypatch: pytest.MonkeyPatch) -> Iterator[PaymentsServer]:
    """A real payments-mock on an ephemeral port; ``PAYMENTS_URL`` points at it."""
    server, url = serve_in_thread()
    monkeypatch.setenv("PAYMENTS_URL", url)
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
