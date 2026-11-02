"""Small helpers over Temporal protos: payload decoding and times (no data converter needed)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from google.protobuf.timestamp_pb2 import Timestamp
from temporalio.api.common.v1 import Payload, Payloads


def decode(payload: Payload | None) -> Any:
    """A JSON payload's value (``json/plain``), ``None`` for null/binary/empty ones."""
    if payload is None or not payload.data:
        return None
    encoding = payload.metadata.get("encoding", b"").decode("utf-8", "replace")
    if encoding not in ("json/plain", "json/protobuf", ""):
        return None
    try:
        return json.loads(payload.data)
    except ValueError:
        return payload.data.decode("utf-8", "replace")


def decode_all(payloads: Payloads | None) -> list[Any]:
    if payloads is None:
        return []
    return [decode(p) for p in payloads.payloads]


def first(payloads: Payloads | None) -> Any:
    values = decode_all(payloads)
    return values[0] if values else None


def summary(payload: Payload) -> str:
    """User-metadata summary text ("" when none)."""
    value = decode(payload)
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def ts(stamp: Timestamp) -> datetime | None:
    """A protobuf timestamp as an aware UTC datetime, ``None`` when unset."""
    if not stamp.seconds and not stamp.nanos:
        return None
    return stamp.ToDatetime(tzinfo=UTC)


def iso(when: datetime | None) -> str | None:
    """RFC 3339 UTC with milliseconds (DESIGN.md 5)."""
    if when is None:
        return None
    return when.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def now() -> datetime:
    return datetime.now(UTC)


def seconds(delta_from: datetime | None, delta_to: datetime | None) -> float | None:
    if delta_from is None or delta_to is None:
        return None
    return round((delta_to - delta_from).total_seconds(), 3)
