"""``scripts/build-id.sh`` is a content hash (plan 3.3, ADR-0004; plan v0.7 acceptance).

In a throwaway clone of this repo: the ID is 7 hex characters, unchanged by a docs-only
commit and by a Finance-only commit, changed by a Support commit, and refused while a
Support file is dirty.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parent.parent


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _build_id(cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "scripts/build-id.sh"], cwd=cwd, capture_output=True, text=True, check=False
    )


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv")
def test_build_id_is_a_content_hash(tmp_path: Path) -> None:
    clone = tmp_path / "repo"
    _git(tmp_path, "clone", "-q", str(ROOT), str(clone))
    # The clone has HEAD's tree; use the working copy's script (it may be newer than HEAD).
    shutil.copy(ROOT / "scripts/build-id.sh", clone / "scripts/build-id.sh")
    _git(clone, "add", "scripts/build-id.sh")
    _git(clone, "commit", "-q", "--allow-empty", "-m", "test: current build-id.sh")

    first = _build_id(clone)
    assert first.returncode == 0, first.stderr
    build = first.stdout.strip()
    assert re.fullmatch(r"[0-9a-f]{7}", build)

    (clone / "docs/story/scratch.md").write_text("docs only\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "docs: docs-only commit")
    assert _build_id(clone).stdout.strip() == build, "docs-only commit changed the Build ID"

    with (clone / "services/finance-agent/README-scratch.md").open("w") as f:
        f.write("finance only\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "chore(finance): finance-only commit")
    assert _build_id(clone).stdout.strip() == build, "Finance-only commit changed the Build ID"

    support_file = clone / "services/support-agent/src/support_agent/config.py"
    support_file.write_text(support_file.read_text() + "\n# a Support change\n")
    dirty = _build_id(clone)
    assert dirty.returncode != 0 and "refusing" in dirty.stderr

    _git(clone, "commit", "-q", "-am", "feat(support): change")
    changed = _build_id(clone).stdout.strip()
    assert re.fullmatch(r"[0-9a-f]{7}", changed) and changed != build
