"""Every timestamp stored in the database is UTC (naive Python datetimes
that represent UTC wall-clock time — this is what Traccar returns, what
`datetime.now(timezone.utc)` produces, and what Postgres stores them as).

Displaying one of those directly with .strftime() shows the *UTC* clock
time, not the UK's — during British Summer Time (UTC+1), that's exactly
an hour behind what a person in the UK would expect to see. This module
is the one place that conversion happens, so every part of the app uses
the same logic rather than six slightly-different copies of it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

UK_TZ = ZoneInfo("Europe/London")


def to_uk(dt: datetime | None) -> datetime | None:
    """A stored (UTC) datetime, converted to UK local time — GMT or BST,
    whichever currently applies, handled automatically by zoneinfo rather
    than a hardcoded offset that would be wrong for half the year."""
    if dt is None:
        return None
    aware_utc = dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    return aware_utc.astimezone(UK_TZ)


def uk_time_str(dt: datetime | None, fmt: str = "%H:%M") -> str:
    """For direct use in plain Python (PDFs, etc) where a Jinja filter
    isn't available."""
    converted = to_uk(dt)
    return converted.strftime(fmt) if converted else ""


def utc_iso(dt: datetime | None) -> str | None:
    """For sending a stored datetime to the browser as JSON — ensures the
    ISO string always carries an explicit UTC marker, so JavaScript's
    own Date parsing (used for the fleet map's 'time ago' and similar)
    correctly treats it as UTC rather than silently assuming it's
    already in the browser's local time."""
    if dt is None:
        return None
    aware_utc = dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    return aware_utc.isoformat()
