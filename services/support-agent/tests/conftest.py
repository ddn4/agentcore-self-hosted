"""Fixtures for the Support test suite."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest


@pytest.fixture
def activity_executor() -> Iterator[ThreadPoolExecutor]:
    """Thread pool for the synchronous Support Activities."""
    with ThreadPoolExecutor(max_workers=4) as executor:
        yield executor
