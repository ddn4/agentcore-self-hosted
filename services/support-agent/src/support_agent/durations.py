"""Durations as people write them in ``.env``: ``24h``, ``30m``, ``5s``, ``1h30m``, ``1d``.

Pure and deterministic, so it is safe in Workflow code: Support's Nexus budget
(``SUPPORT_NEXUS_SEND_TIMEOUT``) reaches ``SupportSupervisorWorkflow`` as this
text (a Workflow input, visible in history) and the Workflow parses it. Team
Finance has its own copy on purpose: the only code the teams share is the
contract ``finance_a2a_api`` (plan §2.2).
"""

from __future__ import annotations

import re
from datetime import timedelta

_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)")
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def parse_duration(text: str) -> timedelta:
    """``"24h"`` -> 24 hours. A bare number is seconds. Raises ``ValueError`` if not positive."""
    raw = text.strip().lower().replace(" ", "")
    if not raw:
        raise ValueError("empty duration")
    if re.fullmatch(r"\d+(?:\.\d+)?", raw):
        seconds = float(raw)
    else:
        parts = _PART.findall(raw)
        if not parts or "".join(n + u for n, u in parts) != raw:
            raise ValueError(f"not a duration: {text!r} (use e.g. 24h, 30m, 5s, 1h30m)")
        seconds = sum(float(n) * _UNIT_SECONDS[u] for n, u in parts)
    if seconds <= 0:
        raise ValueError(f"duration must be positive: {text!r}")
    return timedelta(seconds=seconds)


def format_duration(value: timedelta) -> str:
    """The shortest exact text for a duration: ``24h``, ``90m``, ``5s``."""
    seconds = value.total_seconds()
    for unit in ("h", "m"):
        size = _UNIT_SECONDS[unit]
        if seconds >= size and seconds % size == 0:
            return f"{int(seconds // size)}{unit}"
    return f"{seconds:g}s"
