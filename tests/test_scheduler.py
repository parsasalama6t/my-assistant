from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from assistant.config import Config
from assistant.scheduler import Scheduler
from assistant.schedule_rules import to_utc_iso
from assistant.store import Store
from tests.fakes import FakeClock, FakeGoogleCalendar, FakeRenderer, FakeSender, strict_renderer

TORONTO = ZoneInfo("America/Toronto")


def utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


@pytest.fixture
def cfg() -> Config:
    return Config(
        timezone="America/Toronto",
        default_channel="telegram",
        telegram_bot_token="tok",
        telegram_chat_ids=["42"],
        morning_briefing="07:30",
        evening_review="21:00",
        event_lead_minutes=15,
        task_lead_minutes=0,
        catchup_grace_minutes=120,
    )


def make(store: Store, cfg: Config, clock: FakeClock, render=None, google=None, sender=None):
    sender = sender or FakeSender()
    sched = Scheduler(
        store, cfg, send=sender, render_briefing=render or strict_renderer, google=google,
        now_fn=clock.now, log=logging.getLogger("test.scheduler"),
    )
    return sched, sender


# --------------------------------------------------------------- tick
def test_due_custom_row_sends_exactly_once(store: Store, cfg: Config) -> None:
    clock = FakeClock(utc(2026, 9, 11, 12, 0))
    sched, sender = make(store, cfg, clock)
    row = store.add_schedule("custom", "telegram", "42", to_utc_iso(utc(2026, 9, 11, 11, 59)), "America/Toronto", text="Stretch!")
    first = sched.tick()
    assert [s[2] for s in sender.sent] == ["Stretch!"]
    assert first.sent[0]["id"] == row["id"] and first.sent[0]["provider_message_id"] == "msg1"
    second = sched.tick()
    assert second.sent == [] and second.skipped == [] and len(sender.sent) == 1
    assert store.get_schedule(row["id"])["enabled"] == 0  # one-shot disables
    deliveries = store.list_deliveries()
    assert len(deliveries) == 1 and deliveries[0]["status"] == "sent"


def test_not_due_rows_are_left_alone(store: Store, cfg: Config) -> None:
    clock = FakeClock(utc(2026, 9, 11, 12, 0))
    sched, sender = make(store, cfg, clock)
    store.add_schedule("custom", "telegram", "42", to_utc_iso(utc(2026, 9, 11, 12, 1)), "America/Toronto", text="later")
    assert sched.tick().sent == [] and sender.sent == []
    clock.advance(minutes=1)
    assert len(sched.tick().sent) == 1


def test_recurring_advances_and_fires_next_day(store: Store, cfg: Config) -> None:
    # 08:00 Toronto (EDT) == 12:00Z
    clock = FakeClock(utc(2026, 9, 11, 12, 0))
    sched, sender = make(store, cfg, clock)
    row = store.add_schedule(
        "custom", "telegram", "42", to_utc_iso(utc(2026, 9, 11, 12, 0)), "America/Toronto",
        text="Drink water", repeat="daily", local_time="08:00",
    )
    report = sched.tick()
    assert len(report.sent) == 1
    after = store.get_schedule(row["id"])
    assert after["enabled"] == 1 and after["next_run_utc"] == "2026-09-12T12:00:00+00:00"
    assert after["last_sent_utc"] == "2026-09-11T12:00:00+00:00"
    clock.advance(minutes=60 * 23)
    assert sched.tick().sent == []
    clock.advance(minutes=60)
    assert len(sched.tick().sent) == 1 and len(sender.sent) == 2
    assert store.get_schedule(row["id"])["next_run_utc"] == "2026-09-13T12:00:00+00:00"


def test_late_recurring_is_skipped_but_late_custom_is_sent_with_prefix(store: Store, cfg: Config) -> None:
    fire = utc(2026, 9, 11, 12, 0)  # 08:00 Toronto
    clock = FakeClock(fire)
    clock.advance(minutes=cfg.catchup_grace_minutes + 1)
    sched, sender = make(store, cfg, clock)
    recurring = store.add_schedule("custom", "telegram", "42", to_utc_iso(fire), "America/Toronto", text="daily thing", repeat="daily", local_time="08:00")
    task = store.add_schedule("task_reminder", "telegram", "42", to_utc_iso(fire), "America/Toronto", text="Reminder: x is due now.", source="task:1")
    one_shot = store.add_schedule("custom", "telegram", "42", to_utc_iso(fire), "America/Toronto", text="Call the bank")

    report = sched.tick()
    assert {s["id"] for s in report.skipped} == {recurring["id"], task["id"]}
    assert all(s["reason"] == "late" for s in report.skipped)
    assert [s["id"] for s in report.sent] == [one_shot["id"]]
    assert sender.sent == [("telegram", "42", "(late, this was scheduled for 08:00) Call the bank")]
    # Recurring row still moved on to tomorrow; task row is now disabled.
    assert store.get_schedule(recurring["id"])["next_run_utc"] == "2026-09-12T12:00:00+00:00"
    assert store.get_schedule(task["id"])["enabled"] == 0
    statuses = {d["schedule_id"]: d["status"] for d in store.list_deliveries()}
    assert statuses[recurring["id"]] == "skipped" and statuses[one_shot["id"]] == "sent"


def test_slightly_late_rows_send_without_prefix(store: Store, cfg: Config) -> None:
    fire = utc(2026, 9, 11, 12, 0)
    clock = FakeClock(fire)
    clock.advance(minutes=5)
    sched, sender = make(store, cfg, clock)
    store.add_schedule("custom", "telegram", "42", to_utc_iso(fire), "America/Toronto", text="on time-ish")
    sched.tick()
    assert sender.sent[0][2] == "on time-ish"


def test_send_failure_is_recorded_and_not_retried_for_same_fire(store: Store, cfg: Config) -> None:
    clock = FakeClock(utc(2026, 9, 11, 12, 0))
    sender = FakeSender()
    sender.fail_with = ConnectionError("telegram down")
    sched, _ = make(store, cfg, clock, sender=sender)
    row = store.add_schedule("custom", "telegram", "42", to_utc_iso(utc(2026, 9, 11, 12, 0)), "America/Toronto", text="x")
    report = sched.tick()
    assert report.failed[0]["id"] == row["id"] and "telegram down" in report.failed[0]["error"]
    assert store.list_deliveries()[0]["status"] == "failed"
    sender.fail_with = None
    assert sched.tick().sent == []  # the one-shot was consumed


def test_fixed_text_kinds_never_call_renderer(store: Store, cfg: Config) -> None:
    clock = FakeClock(utc(2026, 9, 11, 12, 0))
    sched, sender = make(store, cfg, clock, render=strict_renderer)
    at = to_utc_iso(utc(2026, 9, 11, 12, 0))
    store.add_schedule("custom", "telegram", "42", at, "America/Toronto", text="custom")
    store.add_schedule("task_reminder", "telegram", "42", at, "America/Toronto", text="task", source="task:1")
    store.add_schedule("event_reminder", "telegram", "42", at, "America/Toronto", text="event", source="event:1")
    report = sched.tick()
    assert len(report.sent) == 3 and report.failed == []
    assert sorted(s[2] for s in sender.sent) == ["custom", "event", "task"]


def test_briefing_kind_uses_renderer(store: Store, cfg: Config) -> None:
    clock = FakeClock(utc(2026, 9, 11, 11, 30))
    renderer = FakeRenderer("Today: dentist at 9")
    sched, sender = make(store, cfg, clock, render=renderer)
    store.add_schedule("briefing", "telegram", "42", to_utc_iso(utc(2026, 9, 11, 11, 30)), "America/Toronto", repeat="daily", local_time="07:30", source="system:morning")
    store.add_schedule("review", "telegram", "42", to_utc_iso(utc(2026, 9, 11, 11, 30)), "America/Toronto", repeat="daily", local_time="07:30", source="system:evening")
    report = sched.tick()
    assert len(report.sent) == 2
    assert {c[0] for c in renderer.calls} == {"briefing", "review"}
    assert renderer.calls[0][1:] == ("telegram", "42")
    assert "Today: dentist at 9 [briefing]" in {s[2] for s in sender.sent}


def test_renderer_failure_is_a_failed_delivery(store: Store, cfg: Config) -> None:
    clock = FakeClock(utc(2026, 9, 11, 11, 30))

    def boom(kind, channel, chat_id):
        raise RuntimeError("api down")

    sched, sender = make(store, cfg, clock, render=boom)
    store.add_schedule("briefing", "telegram", "42", to_utc_iso(clock.now()), "America/Toronto", repeat="daily", local_time="07:30", source="system:morning")
    report = sched.tick()
    assert len(report.failed) == 1 and sender.sent == []
    assert store.list_deliveries()[0]["error"] == "api down"


# ------------------------------------------------------- auto reminders
def test_task_with_due_time_gets_reminder_and_done_removes_it(store: Store, cfg: Config) -> None:
    clock = FakeClock(utc(2026, 9, 11, 12, 0))  # 08:00 Toronto
    sched, sender = make(store, cfg, clock)
    timed = store.add_task("Submit report", due="2026-09-11T14:00")
    store.add_task("Someday", due="2026-09-11")  # date only: no reminder
    store.add_task("No due")
    sched.sync_auto_reminders()
    rows = store.schedules_by_source_prefix("task:")
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == f"task:{timed['id']}" and row["kind"] == "task_reminder"
    assert row["next_run_utc"] == "2026-09-11T18:00:00+00:00"  # 14:00 EDT
    assert row["text"] == "Reminder: Submit report is due now."
    assert row["channel"] == "telegram" and row["chat_id"] == "42"

    # Nothing due yet.
    assert sched.tick().sent == []
    # Rescheduling the task moves the reminder.
    store.update_task(timed["id"], due="2026-09-11T15:30")
    sched.sync_auto_reminders()
    assert store.schedules_by_source_prefix("task:")[0]["next_run_utc"] == "2026-09-11T19:30:00+00:00"
    # Marking it done removes the row.
    store.update_task(timed["id"], status="done")
    sched.sync_auto_reminders()
    assert store.schedules_by_source_prefix("task:") == []


def test_task_reminder_fires_once_and_is_not_recreated(store: Store, cfg: Config) -> None:
    clock = FakeClock(utc(2026, 9, 11, 12, 0))
    sched, sender = make(store, cfg, clock)
    store.add_task("Pay rent", due="2026-09-11T08:05")  # 12:05Z
    sched.sync_auto_reminders()
    clock.advance(minutes=5)
    assert len(sched.run_once().sent) == 1
    assert sender.sent[0][2] == "Reminder: Pay rent is due now."
    clock.advance(minutes=1)
    assert sched.run_once().sent == []  # re-sync must not re-enable a sent one-shot
    assert len(sender.sent) == 1


def test_task_lead_changes_text(store: Store, cfg: Config) -> None:
    cfg.task_lead_minutes = 10
    clock = FakeClock(utc(2026, 9, 11, 12, 0))
    sched, _ = make(store, cfg, clock)
    store.add_task("Leave", due="2026-09-11T09:00")  # 13:00Z
    sched.sync_auto_reminders()
    row = store.schedules_by_source_prefix("task:")[0]
    assert row["next_run_utc"] == "2026-09-11T12:50:00+00:00"
    assert row["text"] == "Reminder: Leave is due in 10 min."


def test_local_event_reminder_at_lead(store: Store, cfg: Config) -> None:
    clock = FakeClock(utc(2026, 9, 11, 12, 0))
    sched, _ = make(store, cfg, clock)
    ev = store.add_event("Dentist", "2026-09-11T10:00", location="Downtown")
    store.add_event("Trip", "2026-10-01")  # all-day, no reminder
    sched.sync_auto_reminders()
    rows = store.schedules_by_source_prefix("event:")
    assert len(rows) == 1
    assert rows[0]["source"] == f"event:{ev['id']}" and rows[0]["kind"] == "event_reminder"
    assert rows[0]["next_run_utc"] == "2026-09-11T13:45:00+00:00"  # 09:45 EDT
    assert rows[0]["text"] == "In 15 min: Dentist (10:00) @ Downtown"
    store.delete_event(ev["id"])
    sched.sync_auto_reminders()
    assert store.schedules_by_source_prefix("event:") == []


def test_google_event_reminder(store: Store, cfg: Config) -> None:
    clock = FakeClock(utc(2026, 9, 11, 12, 0))
    google = FakeGoogleCalendar(
        [
            {"id": "g1", "title": "Standup", "start": "2026-09-11T11:00:00-04:00", "all_day": False, "location": ""},
            {"id": "g2", "title": "Holiday", "start": "2026-09-12", "all_day": True},
        ]
    )
    sched, sender = make(store, cfg, clock, google=google)
    sched.sync_auto_reminders()
    assert google.calls[0][0] == "2026-09-11T08:00" and google.calls[0][1] == "2026-09-12T20:00"
    rows = store.schedules_by_source_prefix("gcal:")
    assert len(rows) == 1 and rows[0]["source"] == "gcal:g1"
    assert rows[0]["next_run_utc"] == "2026-09-11T14:45:00+00:00"
    assert rows[0]["text"] == "In 15 min: Standup (11:00)"
    clock.set(utc(2026, 9, 11, 14, 45))
    assert len(sched.tick().sent) == 1 and sender.sent[0][2] == "In 15 min: Standup (11:00)"
    google.events = []
    sched.sync_auto_reminders()
    assert store.schedules_by_source_prefix("gcal:") == []


def test_google_failure_keeps_existing_rows(store: Store, cfg: Config) -> None:
    clock = FakeClock(utc(2026, 9, 11, 12, 0))
    google = FakeGoogleCalendar([{"id": "g1", "title": "X", "start": "2026-09-11T11:00:00-04:00", "all_day": False}])
    sched, _ = make(store, cfg, clock, google=google)
    sched.sync_auto_reminders()
    assert len(store.schedules_by_source_prefix("gcal:")) == 1

    def broken(*a, **k):
        raise ConnectionError("offline")

    google.list_events = broken  # type: ignore[assignment]
    sched.sync_auto_reminders()
    assert len(store.schedules_by_source_prefix("gcal:")) == 1


def test_past_beyond_grace_is_not_scheduled(store: Store, cfg: Config) -> None:
    clock = FakeClock(utc(2026, 9, 11, 18, 0))  # 14:00 Toronto
    sched, _ = make(store, cfg, clock)
    store.add_task("Old", due="2026-09-11T09:00")  # 5 hours ago
    store.add_task("Recent", due="2026-09-11T13:00")  # 1 hour ago, within grace
    sched.sync_auto_reminders()
    rows = store.schedules_by_source_prefix("task:")
    assert [r["text"] for r in rows] == ["Reminder: Recent is due now."]


def test_sync_without_target_does_nothing(store: Store) -> None:
    cfg = Config(timezone="America/Toronto")
    clock = FakeClock(utc(2026, 9, 11, 12, 0))
    sched, _ = make(store, cfg, clock)
    store.add_task("x", due="2026-09-11T14:00")
    sched.sync_auto_reminders()
    assert store.list_schedules(include_system=True, enabled_only=False) == []


# ------------------------------------------------------ fixed schedules
def test_ensure_fixed_schedules_create_update_delete(store: Store, cfg: Config) -> None:
    clock = FakeClock(utc(2026, 9, 11, 12, 0))  # 08:00 Toronto, past 07:30
    sched, _ = make(store, cfg, clock)
    sched.ensure_fixed_schedules()
    rows = {r["source"]: r for r in store.schedules_by_source_prefix("system:")}
    assert set(rows) == {"system:morning", "system:evening"}
    m, e = rows["system:morning"], rows["system:evening"]
    assert m["kind"] == "briefing" and m["repeat"] == "daily" and m["local_time"] == "07:30"
    assert m["next_run_utc"] == "2026-09-12T11:30:00+00:00"
    assert e["kind"] == "review" and e["next_run_utc"] == "2026-09-12T01:00:00+00:00"
    assert m["channel"] == "telegram" and m["chat_id"] == "42" and m["tz"] == "America/Toronto"

    # Unchanged config keeps the same rows and their next run.
    sched.ensure_fixed_schedules()
    again = {r["source"]: r for r in store.schedules_by_source_prefix("system:")}
    assert again["system:morning"]["id"] == m["id"] and again["system:morning"]["next_run_utc"] == m["next_run_utc"]

    # Changing the time updates in place.
    cfg.morning_briefing = "09:00"
    sched.ensure_fixed_schedules()
    updated = store.get_schedule(m["id"])
    assert updated["local_time"] == "09:00" and updated["next_run_utc"] == "2026-09-11T13:00:00+00:00"

    # Empty time deletes.
    cfg.evening_review = ""
    sched.ensure_fixed_schedules()
    assert [r["source"] for r in store.schedules_by_source_prefix("system:")] == ["system:morning"]


def test_ensure_fixed_schedules_respects_user_cancel(store: Store, cfg: Config) -> None:
    clock = FakeClock(utc(2026, 9, 11, 12, 0))
    sched, _ = make(store, cfg, clock)
    sched.ensure_fixed_schedules()
    row = store.schedules_by_source_prefix("system:evening")[0]
    store.cancel_schedule(row["id"])
    sched.ensure_fixed_schedules()
    assert store.get_schedule(row["id"])["enabled"] == 0
    cfg.evening_review = "20:00"
    sched.ensure_fixed_schedules()
    assert store.get_schedule(row["id"])["enabled"] == 1


def test_ensure_fixed_schedules_keeps_pending_catchup(store: Store, cfg: Config) -> None:
    clock = FakeClock(utc(2026, 9, 10, 12, 0))
    sched, _ = make(store, cfg, clock)
    sched.ensure_fixed_schedules()  # morning -> 2026-09-11T11:30Z
    clock.set(utc(2026, 9, 11, 11, 40))  # daemon restarts 10 min after the briefing time
    sched.ensure_fixed_schedules()
    row = store.schedules_by_source_prefix("system:morning")[0]
    assert row["next_run_utc"] == "2026-09-11T11:30:00+00:00"


# ---------------------------------------------------------------- loops
def test_run_forever_recovers_stale_deliveries_and_stops(store: Store, cfg: Config) -> None:
    clock = FakeClock(utc(2026, 9, 11, 12, 0))
    row = store.add_schedule("custom", "telegram", "42", to_utc_iso(utc(2026, 9, 11, 11, 0)), "America/Toronto", text="old")
    store.claim_delivery(row["id"], row["next_run_utc"])
    store.conn.execute("UPDATE deliveries SET created_at = '2026-09-11T11:00:00+00:00'")
    store.conn.commit()
    cfg.tick_seconds = 1
    renderer = FakeRenderer()
    sched, sender = make(store, cfg, clock, render=renderer)
    stop = threading.Event()
    stop.set()  # run exactly one iteration
    sched.run_forever(stop)
    assert store.list_deliveries()[0]["status"] == "failed"
    assert {r["source"] for r in store.schedules_by_source_prefix("system:")} == {"system:morning", "system:evening"}
    assert sender.sent == []  # the stale row's firing was already claimed
