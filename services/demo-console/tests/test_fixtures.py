"""The recorded fixtures stay on the backend's contract (``make console-fixtures``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from demo_console.fixtures import DERIVED, FIXTURES_DIR, derived, dump

pytestmark = pytest.mark.unit

FIXTURES = sorted(FIXTURES_DIR.glob("*.json"))


def test_there_are_fixtures() -> None:
    assert len(FIXTURES) >= 5


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_fixture_matches_what_the_backend_serves(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    fixture = json.loads(text)
    # The on-disk format: indent=1, UTF-8 as is, a final newline.
    assert dump(fixture) == text, f"{path.name}: run make console-fixtures"
    want = derived(fixture)
    for part in DERIVED:
        assert fixture[part] == want[part], (
            f"{path.name}: {part} drifted from the backend; run make console-fixtures"
        )
