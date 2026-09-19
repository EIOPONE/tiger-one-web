"""The bug this fixes: a UTC timestamp stored in the database was being
displayed as-is, as if it were already UK local time. During British
Summer Time (UTC+1), that showed everything an hour behind reality.
"""
from datetime import datetime, timezone

from app import tz


def test_bst_summer_time_is_an_hour_ahead_of_stored_utc():
    """The actual bug, reproduced directly: 13:00 UTC on a September day
    (BST in effect) must display as 14:00, not 13:00."""
    stored_utc = datetime(2026, 9, 13, 13, 0, tzinfo=timezone.utc)
    assert tz.uk_time_str(stored_utc) == "14:00"


def test_gmt_winter_time_matches_utc_exactly():
    """In winter (GMT, no offset from UTC), the displayed time should
    match the stored UTC time exactly — confirms this isn't a hardcoded
    +1 hour that would then be wrong for half the year."""
    stored_utc = datetime(2026, 1, 13, 13, 0, tzinfo=timezone.utc)
    assert tz.uk_time_str(stored_utc) == "13:00"


def test_handles_naive_datetimes_as_utc():
    """Stored datetimes come back from the database naive (no tzinfo
    attached) but numerically represent UTC — must be treated as UTC,
    not as if they were already local."""
    naive_utc = datetime(2026, 9, 13, 13, 0)  # no tzinfo, as read from the DB
    assert naive_utc.tzinfo is None
    assert tz.uk_time_str(naive_utc) == "14:00"


def test_none_returns_empty_string_not_an_error():
    assert tz.uk_time_str(None) == ""


def test_utc_iso_always_includes_a_timezone_marker():
    """The JSON-for-JavaScript path — a naive stored datetime must come
    out with an explicit UTC offset, so the browser's own Date parsing
    doesn't silently misinterpret it as local time."""
    naive_utc = datetime(2026, 9, 13, 13, 0)
    result = tz.utc_iso(naive_utc)
    assert result is not None
    assert "+00:00" in result or result.endswith("Z")


def test_utc_iso_none_stays_none():
    assert tz.utc_iso(None) is None


def test_to_uk_preserves_the_correct_instant_in_time():
    """Converting to UK time must not change *when* something happened —
    only how it's displayed. The underlying instant, compared in UTC,
    must be identical before and after conversion."""
    stored_utc = datetime(2026, 9, 13, 13, 0, tzinfo=timezone.utc)
    converted = tz.to_uk(stored_utc)
    assert converted.astimezone(timezone.utc) == stored_utc
