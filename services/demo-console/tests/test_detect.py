"""Profile detection from pidfiles and the TLS probe (DESIGN.md 4.2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from demo_console.detect import detect, mismatch

pytestmark = pytest.mark.unit


def pidfiles(tmp_path: Path, **pids: int) -> Path:
    for name, pid in pids.items():
        (tmp_path / f"{name.replace('_', '-')}.pid").write_text(f"{pid}\n")
    return tmp_path


async def probe_mtls() -> bool | None:
    return False


async def probe_plain() -> bool | None:
    return True


async def test_nothing(tmp_path: Path) -> None:
    found = await detect(tmp_path, lambda pid: True, probe_mtls)
    assert found.detected is None and found.port_forward is None


async def test_dev_server(tmp_path: Path) -> None:
    pidfiles(tmp_path, dev_server=101)
    assert (await detect(tmp_path, lambda pid: pid == 101, probe_mtls)).detected == "local"
    # A stale pidfile (kill -0 fails) is ignored.
    assert (await detect(tmp_path, lambda pid: False, probe_mtls)).detected is None


async def test_kind_mtls_vs_plaintext(tmp_path: Path) -> None:
    pidfiles(tmp_path, kind_pf_frontend=201, kind_pf_web=202)
    found = await detect(tmp_path, lambda pid: True, probe_mtls)
    assert found.detected == "kind"
    assert found.port_forward == {"kind": "kind", "frontend": True, "web": True}
    assert (await detect(tmp_path, lambda pid: True, probe_plain)).detected == "kind-plaintext"


async def test_eks_pidfiles_are_ignored(tmp_path: Path) -> None:
    """The EKS profile is gone: a leftover eks-pf pidfile detects nothing."""
    pidfiles(tmp_path, eks_pf_frontend=301)
    assert (await detect(tmp_path, lambda pid: pid == 301, probe_mtls)).detected is None


def test_mismatch_text() -> None:
    assert mismatch("local", "local") is None and mismatch("local", None) is None
    assert mismatch("local", "kind") == (
        "A kind port-forward is running, but the console is on profile local. Switch to kind."
    )
