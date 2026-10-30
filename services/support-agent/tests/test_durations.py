"""Unit tests: the duration text used for SUPPORT_NEXUS_SEND_TIMEOUT (Workflow-safe parser)."""

from datetime import timedelta

import pytest

from support_agent.durations import format_duration, parse_duration

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("24h", timedelta(hours=24)),
        ("5s", timedelta(seconds=5)),
        ("30m", timedelta(minutes=30)),
        ("1h30m", timedelta(minutes=90)),
        ("1d", timedelta(days=1)),
        ("250ms", timedelta(milliseconds=250)),
        ("90", timedelta(seconds=90)),
        (" 2H ", timedelta(hours=2)),
    ],
)
def test_parse(text: str, expected: timedelta) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "h", "5x", "1h junk", "-1h", "0", "0s"])
def test_parse_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        parse_duration(text)


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (timedelta(hours=24), "24h"),
        (timedelta(hours=25), "25h"),
        (timedelta(minutes=90), "90m"),
        (timedelta(seconds=5), "5s"),
    ],
)
def test_format_round_trips(value: timedelta, text: str) -> None:
    assert format_duration(value) == text
    assert parse_duration(text) == value
