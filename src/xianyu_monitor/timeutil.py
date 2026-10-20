"""UTC time helpers. All persisted timestamps are naive UTC datetimes."""

from __future__ import annotations

import datetime as dt


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(microsecond=value.microsecond).isoformat(timespec="seconds") + "Z"
