"""In-process broadcast of push events (``job``, ``process``, ``alert``) to SSE subscribers."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from typing import Any

Event = tuple[str, dict[str, Any]]


class EventBus:
    def __init__(self, maxsize: int = 256) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._maxsize = maxsize

    def publish(self, event: str, data: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait((event, data))
            except asyncio.QueueFull:  # a stalled client misses pushes; snapshots catch up
                pass

    def alert(self, level: str, message: str, hint: str | None = None) -> None:
        self.publish("alert", {"level": level, "message": message, "hint": hint})

    @contextlib.contextmanager
    def subscribe(self) -> Iterator[asyncio.Queue[Event]]:
        queue: asyncio.Queue[Event] = asyncio.Queue(self._maxsize)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)

    @property
    def subscribers(self) -> int:
        return len(self._subscribers)
