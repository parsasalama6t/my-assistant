"""Pure date/time rules for scheduled messages (no I/O).

Conventions used across the app:

* User-facing strings (tasks.due, events.start, tool inputs) are naive local
  ISO strings like ``2026-09-14T15:30``. They are interpreted in the configured
  timezone; ``parse_local_datetime`` is the one place they become aware.
* Scheduler-owned fields (``next_run_utc``, ``last_sent_utc``, ``fire_at_utc``)
  are aware ISO strings in UTC, produced by ``to_utc_iso``.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone, tzinfo

REPEAT_RULES = """\
Repeat grammar (case-insensitive):
  daily                 every day
  weekdays              Monday to Friday
  weekends              Saturday and Sunday
  weekly                same weekday every week (stored as days:<weekday>)
  days:mon,wed,fri      listed weekdays (mon tue wed thu fri sat sun)
Omit the rule for a one-off message."""

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_LOCAL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?)?$")


def parse_repeat(rule: str | None, weekday: int | None = None) -> str | None:
    """Normalize a repeat rule or raise ValueError.

    ``weekly`` needs the weekday of the first occurrence to become ``days:<wd>``;
    pass ``weekday`` (0=Monday) for that, otherwise it is returned as ``weekly``
    and resolved later by ``next_occurrence``.
    """
    if rule is None:
        return None
    text = rule.strip().lower().replace(" ", "")
    if not text:
        return None
    if text in {"daily", "weekdays", "weekends"}:
        return text
    if text == "weekly":
        if weekday is None:
            return "weekly"
        return f"days:{WEEKDAYS[weekday]}"
    if text.startswith("days:"):
        names = [n for n in text[5:].split(",") if n]
        if not names:
            raise ValueError("days: rule needs at least one weekday, e.g. days:mon,wed")
        bad = [n for n in names if n not in WEEKDAYS]
        if bad:
            raise ValueError(f"unknown weekday(s) {', '.join(bad)}; use {', '.join(WEEKDAYS)}")
        ordered = [d for d in WEEKDAYS if d in names]
        return "days:" + ",".join(ordered)
    raise ValueError(f"unknown repeat rule {rule!r}.\n{REPEAT_RULES}")


def _matches(rule: str, day: date) -> bool:
    wd = day.weekday()
    if rule == "daily":
        return True
    if rule == "weekdays":
        return wd < 5
    if rule == "weekends":
        return wd >= 5
    if rule.startswith("days:"):
        return WEEKDAYS[wd] in rule[5:].split(",")
    raise ValueError(f"unknown repeat rule {rule!r}")


def parse_hhmm(local_time: str) -> time:
    try:
        hour, minute = local_time.strip().split(":")
        return time(int(hour), int(minute))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"local_time must be HH:MM; got {local_time!r}") from exc


def next_occurrence(repeat: str, local_time: str, after: datetime, tz: tzinfo) -> datetime:
    """First wall-clock ``local_time`` in ``tz`` strictly after ``after`` matching ``repeat``.

    Returns an aware UTC datetime. ``after`` must be aware. Days are walked from
    ``after`` (in ``tz``) forward, so DST transitions are handled by combining
    the local date with the local time rather than adding fixed offsets.
    """
    if after.tzinfo is None:
        raise ValueError("after must be an aware datetime")
    rule = parse_repeat(repeat)
    if rule is None:
        raise ValueError("next_occurrence needs a repeat rule")
    at = parse_hhmm(local_time)
    start_day = after.astimezone(tz).date()
    if rule == "weekly":
        rule = f"days:{WEEKDAYS[start_day.weekday()]}"
    for offset in range(0, 9):
        day = start_day + timedelta(days=offset)
        if not _matches(rule, day):
            continue
        candidate = datetime.combine(day, at, tzinfo=tz)
        if candidate > after:
            return candidate.astimezone(timezone.utc)
    raise ValueError(f"no occurrence of {repeat!r} within 8 days")  # unreachable for valid rules


def parse_local_datetime(text: str, tz: tzinfo) -> datetime:
    """Turn a user-facing date/time string into an aware datetime.

    Accepts ``YYYY-MM-DD`` (interpreted as 09:00 local), ``YYYY-MM-DDTHH:MM[:SS]``
    (local wall time in ``tz``), or a full ISO string with an offset.
    """
    value = (text or "").strip()
    if not value:
        raise ValueError("date/time is required")
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    if _LOCAL_RE.match(value):
        if "T" not in value:
            dt = datetime.combine(date.fromisoformat(value), time(9, 0))
        else:
            dt = datetime.fromisoformat(value)
        return dt.replace(tzinfo=tz)
    try:
        dt = datetime.fromisoformat(value.replace(" ", "T", 1))
    except ValueError as exc:
        raise ValueError(
            f"could not parse {text!r}; use YYYY-MM-DD or YYYY-MM-DDTHH:MM"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


def to_utc_iso(dt: datetime) -> str:
    """Aware datetime -> ISO string in UTC with second precision."""
    if dt.tzinfo is None:
        raise ValueError("expected an aware datetime")
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def from_utc_iso(value: str) -> datetime:
    """ISO string (as stored by ``to_utc_iso``) -> aware UTC datetime."""
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_local(value: str, tz: tzinfo, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Render a stored UTC ISO string as local wall time for display."""
    return from_utc_iso(value).astimezone(tz).strftime(fmt)
