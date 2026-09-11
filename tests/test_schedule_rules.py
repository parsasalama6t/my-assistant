from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from assistant.schedule_rules import (
    format_local,
    from_utc_iso,
    next_occurrence,
    parse_local_datetime,
    parse_repeat,
    to_utc_iso,
)

TORONTO = ZoneInfo("America/Toronto")


def utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def test_parse_repeat_normalizes() -> None:
    assert parse_repeat(None) is None
    assert parse_repeat("") is None
    assert parse_repeat(" Daily ") == "daily"
    assert parse_repeat("weekdays") == "weekdays"
    assert parse_repeat("weekends") == "weekends"
    assert parse_repeat("days:fri, mon") == "days:mon,fri"
    assert parse_repeat("weekly") == "weekly"
    assert parse_repeat("weekly", weekday=2) == "days:wed"
    for bad in ("hourly", "days:", "days:funday", "every other day"):
        with pytest.raises(ValueError):
            parse_repeat(bad)


def test_daily_next_occurrence_is_strictly_after() -> None:
    # 2026-09-11 is a Friday. 08:00 Toronto (EDT, -4) == 12:00Z.
    after = utc(2026, 9, 11, 11, 0)
    assert next_occurrence("daily", "08:00", after, TORONTO) == utc(2026, 9, 11, 12, 0)
    exactly = utc(2026, 9, 11, 12, 0)
    assert next_occurrence("daily", "08:00", exactly, TORONTO) == utc(2026, 9, 12, 12, 0)


def test_weekday_rules() -> None:
    friday_afternoon = utc(2026, 9, 11, 20, 0)  # 16:00 Toronto, Friday
    # weekdays: Friday 08:00 is gone, next is Monday 2026-09-14.
    assert next_occurrence("weekdays", "08:00", friday_afternoon, TORONTO) == utc(2026, 9, 14, 12, 0)
    # weekends: Saturday.
    assert next_occurrence("weekends", "09:30", friday_afternoon, TORONTO) == utc(2026, 9, 12, 13, 30)
    # explicit days.
    assert next_occurrence("days:wed", "07:00", friday_afternoon, TORONTO) == utc(2026, 9, 16, 11, 0)
    # weekly resolves to the weekday of `after` (Friday), next week.
    assert next_occurrence("weekly", "08:00", friday_afternoon, TORONTO) == utc(2026, 9, 18, 12, 0)


def test_dst_spring_forward_toronto() -> None:
    # DST starts 2026-03-08 at 02:00 local; 07:30 that morning is EDT (UTC-4).
    after = utc(2026, 3, 7, 13, 0)  # 08:00 EST on the 7th, already past 07:30
    assert next_occurrence("daily", "07:30", after, TORONTO) == utc(2026, 3, 8, 11, 30)
    # The day before it was 12:30Z (EST).
    before = utc(2026, 3, 6, 13, 0)
    assert next_occurrence("daily", "07:30", before, TORONTO) == utc(2026, 3, 7, 12, 30)


def test_dst_fall_back_toronto() -> None:
    # DST ends 2026-11-01 at 02:00 local; 07:30 that morning is EST (UTC-5).
    after = utc(2026, 10, 31, 12, 0)  # 08:00 EDT on Oct 31
    assert next_occurrence("daily", "07:30", after, TORONTO) == utc(2026, 11, 1, 12, 30)
    before = utc(2026, 10, 30, 12, 0)
    assert next_occurrence("daily", "07:30", before, TORONTO) == utc(2026, 10, 31, 11, 30)


def test_next_occurrence_requires_aware_and_rule() -> None:
    with pytest.raises(ValueError):
        next_occurrence("daily", "07:00", datetime(2026, 1, 1), TORONTO)
    with pytest.raises(ValueError):
        next_occurrence("", "07:00", utc(2026, 1, 1), TORONTO)
    with pytest.raises(ValueError):
        next_occurrence("daily", "7pm", utc(2026, 1, 1), TORONTO)


def test_parse_local_datetime_forms() -> None:
    d = parse_local_datetime("2026-09-14", TORONTO)
    assert (d.hour, d.minute) == (9, 0) and d.tzinfo is TORONTO
    t = parse_local_datetime("2026-09-14T15:30", TORONTO)
    assert t == datetime(2026, 9, 14, 15, 30, tzinfo=TORONTO)
    assert to_utc_iso(t) == "2026-09-14T19:30:00+00:00"
    s = parse_local_datetime("2026-09-14T15:30:45", TORONTO)
    assert s.second == 45
    with_offset = parse_local_datetime("2026-09-14T15:30:00-07:00", TORONTO)
    assert to_utc_iso(with_offset) == "2026-09-14T22:30:00+00:00"
    z = parse_local_datetime("2026-09-14T15:30:00Z", TORONTO)
    assert to_utc_iso(z) == "2026-09-14T15:30:00+00:00"
    for bad in ("", "tomorrow", "2026-13-01", "14/09/2026"):
        with pytest.raises(ValueError):
            parse_local_datetime(bad, TORONTO)


def test_utc_iso_roundtrip_and_format_local() -> None:
    dt = datetime(2026, 9, 14, 15, 30, 12, 345, tzinfo=TORONTO)
    iso = to_utc_iso(dt)
    assert iso == "2026-09-14T19:30:12+00:00"
    back = from_utc_iso(iso)
    assert back.tzinfo == timezone.utc and back == dt.replace(microsecond=0)
    assert from_utc_iso("2026-09-14T19:30:12Z") == back
    assert format_local(iso, TORONTO) == "2026-09-14 15:30"
    with pytest.raises(ValueError):
        to_utc_iso(datetime(2026, 1, 1))
