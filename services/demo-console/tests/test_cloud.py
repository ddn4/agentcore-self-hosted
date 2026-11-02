"""Opt-in AWS adapters with fakes (DESIGN.md 4.4): no boto3 client is ever created."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from demo_console.cloud import AwsDisabled, CloudAdapter, failure_argv, session_summary
from demo_console.fakes import make_settings

pytestmark = pytest.mark.unit


class Session:
    def __init__(self, sid: str, released: int | None) -> None:
        self.session_id = sid
        self.events = {"app_start": 1793636000000, "invoked": 1793636000100}
        if released:
            self.events["released"] = released

    def first(self, marker: str) -> int | None:
        return self.events.get(marker)

    def count(self, marker: str) -> int:
        return 1 if marker in self.events else 0

    @property
    def drained(self) -> bool:
        return "released" in self.events


async def test_disabled_without_console_allow_aws(tmp_path: Path) -> None:
    def boom(*_: Any) -> list[Any]:
        raise AssertionError("must not be called")

    cloud = CloudAdapter(
        make_settings(tmp_path, profile="eks-pf"), sessions_for=boom, wait_drained=boom
    )
    with pytest.raises(AwsDisabled):
        await cloud.sessions("support-2077", 0, "4ee7f87")
    with pytest.raises(AwsDisabled):
        await cloud.wait_drained("support-2077", 0, "4ee7f87")


async def test_sessions_and_wait_drained_with_fakes(tmp_path: Path) -> None:
    seen: list[tuple[Any, ...]] = []

    def sessions_for(wf: str, since: int, build: str | None) -> list[Any]:
        seen.append(("sessions", wf, since, build))
        return [Session("aaaaaaaa-1111", 1793636050000), Session("bbbbbbbb-2222", None)]

    def wait_drained(wf: str, since: int, within: float, build: str | None) -> list[Any]:
        seen.append(("wait", wf, since, within, build))
        return [Session("aaaaaaaa-1111", 1793636050000)]

    settings = make_settings(tmp_path, profile="eks-pf")
    settings.allow_aws = True
    cloud = CloudAdapter(settings, sessions_for=sessions_for, wait_drained=wait_drained)
    found = await cloud.sessions("support-2077", 5, "4ee7f87")
    assert [s["session"] for s in found] == ["aaaaaaaa", "bbbbbbbb"]
    assert found[0]["drained"] is True and found[1]["drained"] is False
    assert found[0]["app_start_iso"] == "2026-11-02T16:13:20.000Z"
    drained = await cloud.wait_drained("support-2077", 5, "4ee7f87", within=60)
    assert drained[0]["released_iso"] == "2026-11-02T16:14:10.000Z"
    assert seen == [
        ("sessions", "support-2077", 5, "4ee7f87"),
        ("wait", "support-2077", 5, 60, "4ee7f87"),
    ]


def test_session_summary_and_failure_argv() -> None:
    summary = session_summary(Session("cccccccc-3333", None))
    assert summary["lifetimes"] == 1 and summary["released_iso"] is None
    assert failure_argv("support-1042-failure-x") == [
        "make",
        "-s",
        "failure",
        "WF=support-1042-failure-x",
    ]
