"""How the console runs commands (DESIGN.md 4.5): a protocol, so tests use a fake.

Every child runs in its own session (``start_new_session=True``), so its process group id is
its pid and a stop reaches ``make`` and everything ``make`` started.
"""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class CommandResult:
    argv: list[str]
    returncode: int
    output: str


class Spawned(Protocol):
    """A started child process."""

    @property
    def pid(self) -> int: ...

    async def wait(self) -> int: ...


class CommandRunner(Protocol):
    async def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: Path,
        within: float = 60.0,
    ) -> CommandResult: ...

    async def spawn(
        self, argv: Sequence[str], *, env: Mapping[str, str], cwd: Path, log_path: Path
    ) -> Spawned: ...

    def signal_group(self, pgid: int, sig: int) -> bool: ...

    def group_alive(self, pgid: int) -> bool: ...

    def pid_alive(self, pid: int) -> bool: ...


class _AsyncioSpawned:
    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc

    @property
    def pid(self) -> int:
        return self._proc.pid

    async def wait(self) -> int:
        return await self._proc.wait()


class AsyncioRunner:
    """The real runner: ``asyncio.create_subprocess_exec`` in a new session."""

    async def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: Path,
        within: float = 60.0,
    ) -> CommandResult:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=dict(env),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), within)
        except TimeoutError:
            self.signal_group(proc.pid, signal.SIGKILL)
            await proc.wait()
            return CommandResult(list(argv), 124, f"timed out after {within:g}s")
        return CommandResult(list(argv), proc.returncode or 0, out.decode("utf-8", "replace"))

    async def spawn(
        self, argv: Sequence[str], *, env: Mapping[str, str], cwd: Path, log_path: Path
    ) -> Spawned:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as log:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                env=dict(env),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        return _AsyncioSpawned(proc)

    def signal_group(self, pgid: int, sig: int) -> bool:
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            return False
        return True

    def group_alive(self, pgid: int) -> bool:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def pid_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
