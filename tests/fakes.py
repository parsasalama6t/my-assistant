"""Offline test doubles shared by the scheduler and tool tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


class FakeClock:
    """A controllable UTC clock; pass `clock.now` as the scheduler's now_fn."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        self.current = start.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self.current

    def advance(self, minutes: float = 0, **kwargs: float) -> datetime:
        self.current += timedelta(minutes=minutes, **kwargs)
        return self.current

    def set(self, when: datetime) -> datetime:
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        self.current = when.astimezone(timezone.utc)
        return self.current


class FakeSender:
    """Records outbound texts; can be told to fail."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.fail_with: Exception | None = None

    def __call__(self, channel: str, chat_id: str, text: str) -> str:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append((channel, chat_id, text))
        return f"msg{len(self.sent)}"


class FakeRenderer:
    """Stands in for the Claude-backed briefing renderer and counts calls."""

    def __init__(self, text: str = "Good morning briefing") -> None:
        self.text = text
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, kind: str, channel: str, chat_id: str) -> str:
        self.calls.append((kind, channel, chat_id))
        return f"{self.text} [{kind}]"


def strict_renderer(kind: str, channel: str, chat_id: str) -> str:
    raise AssertionError(f"render_briefing must not be called for {kind}")


class FakeGoogleCalendar:
    """Minimal GoogleClient surface for the scheduler: list_events only."""

    email = "sam@example.com"

    def __init__(self, events: list[dict[str, Any]] | None = None) -> None:
        self.events = events or []
        self.calls: list[tuple[Any, Any, Any]] = []

    def list_events(self, start=None, end=None, limit=25):
        self.calls.append((start, end, limit))
        return list(self.events)


class FakeCaller:
    """Records phone calls the scheduler/daemon place; can be told to fail."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_with: Exception | None = None

    def __call__(self, to_number: str, text: str) -> str:
        if self.fail_with is not None:
            raise self.fail_with
        self.calls.append((to_number, text))
        return f"CA{len(self.calls)}"


class FakeVoice:
    """Stands in for TwilioVoice inside the daemon (`voice=` injection)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    def call(self, to_number: str, text: str, repeat: int = 2) -> str:
        self.calls.append((to_number, text))
        return f"CA{len(self.calls)}"

    def close(self) -> None:
        self.closed = True
