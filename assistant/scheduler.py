"""Timezone-aware scheduler that texts the user at fixed times and before tasks/events.

The daemon (built separately) constructs a ``Scheduler`` with a ``send`` callable
for its messaging channels and drives it with ``run_forever``. Everything here is
deterministic given ``now_fn`` so it can be tested with a fake clock.

Fixed-text rows (custom, task_reminder, event_reminder, call) are sent verbatim
and never touch the model; briefing/review rows ask ``render_briefing`` for
their text.

Priorities: a ``normal`` row is a text. An ``important`` row is a text followed,
``escalate_minutes`` later, by a phone call unless the user replied in between
(the daemon cancels the pending ``escalate:`` row on any inbound message). A
``critical`` row is a text and a call in the same tick. Rows of kind ``call``
only ring the phone; they never send a text.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from assistant.config import Config
from assistant.schedule_rules import (
    from_utc_iso,
    next_occurrence,
    parse_local_datetime,
    to_utc_iso,
)
from assistant.store import AUTO_SOURCE_PREFIXES, ESCALATION_PREFIX, Store

SendFn = Callable[[str, str, str], Any]  # (channel, chat_id, text) -> provider message id
RenderFn = Callable[[str, str, str], str]  # (kind, channel, chat_id) -> text
CallFn = Callable[[str, str], Any]  # (to_number, text) -> provider call id

FIXED_TEXT_KINDS = ("custom", "task_reminder", "event_reminder", "call")
RENDERED_KINDS = ("briefing", "review")
AUTO_KINDS = ("task_reminder", "event_reminder")
SKIP_WHEN_LATE_KINDS = (*AUTO_KINDS, "call")  # a late call is worse than no call
VOICE_NOT_CONFIGURED = "voice not configured"
SYNC_EVERY_TICKS = 5
STALE_SENDING_MINUTES = 10
GOOGLE_LOOKAHEAD = timedelta(hours=36)


@dataclass
class TickReport:
    sent: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)

    def __iadd__(self, other: "TickReport") -> "TickReport":
        self.sent += other.sent
        self.skipped += other.skipped
        self.failed += other.failed
        return self


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Scheduler:
    def __init__(
        self,
        store: Store,
        config: Config,
        send: SendFn,
        render_briefing: RenderFn,
        google: Any | None = None,
        now_fn: Callable[[], datetime] = _utc_now,
        log: logging.Logger | None = None,
        place_call: CallFn | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self.send = send
        self.render_briefing = render_briefing
        self.google = google
        self.now_fn = now_fn
        self.log = log or logging.getLogger("assistant.scheduler")
        self.place_call = place_call

    # ------------------------------------------------------------ helpers
    @property
    def tz(self) -> ZoneInfo:
        return self.config.tz

    @property
    def grace(self) -> timedelta:
        return timedelta(minutes=self.config.catchup_grace_minutes)

    def _now(self) -> datetime:
        now = self.now_fn()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)

    @staticmethod
    def _summary(row: dict[str, Any], **extra: Any) -> dict[str, Any]:
        out = {
            "id": row["id"],
            "kind": row["kind"],
            "priority": row.get("priority") or "normal",
            "source": row.get("source"),
            "channel": row["channel"],
            "chat_id": row["chat_id"],
            "fire_at_utc": row["next_run_utc"],
            "via": "call" if row["kind"] == "call" else "text",
        }
        out.update(extra)
        return out

    def voice_ready(self) -> bool:
        return self.place_call is not None and bool(self.config.user_phone)

    def _render(self, row: dict[str, Any]) -> str:
        if row["kind"] in RENDERED_KINDS:
            return self.render_briefing(row["kind"], row["channel"], row["chat_id"])
        return row["text"]

    def _late_prefix(self, fire_at: datetime, now: datetime) -> str:
        tz = self.tz
        local_fire = fire_at.astimezone(tz)
        when = local_fire.strftime("%H:%M")
        if local_fire.date() != now.astimezone(tz).date():
            when = local_fire.strftime("%Y-%m-%d %H:%M")
        return f"(late, this was scheduled for {when}) "

    # --------------------------------------------------------------- tick
    def tick(self) -> TickReport:
        """Send everything that is due. Safe to call repeatedly; each firing sends once."""
        report = TickReport()
        now = self._now()
        for row in self.store.due_schedules(to_utc_iso(now)):
            fire_at_iso = row["next_run_utc"]
            if not self.store.claim_delivery(row["id"], fire_at_iso):
                report.skipped.append(self._summary(row, reason="already_claimed"))
                continue

            # Advance first so a crash mid-send cannot cause a resend loop.
            next_iso: str | None = None
            if row["repeat"]:
                try:
                    row_tz = ZoneInfo(row["tz"]) if row.get("tz") else self.tz
                    next_iso = to_utc_iso(next_occurrence(row["repeat"], row["local_time"], now, row_tz))
                except (ValueError, KeyError) as exc:
                    self.log.warning("schedule %s has a bad repeat rule (%s); disabling", row["id"], exc)
            self.store.advance_schedule(row["id"], next_iso, to_utc_iso(now))

            fire_at = from_utc_iso(fire_at_iso)
            lateness = now - fire_at
            prefix = ""
            if lateness > self.grace:
                if row["repeat"] or row["kind"] in SKIP_WHEN_LATE_KINDS:
                    self.store.finish_delivery(row["id"], fire_at_iso, "skipped", error="missed beyond grace")
                    report.skipped.append(self._summary(row, reason="late", lateness_minutes=int(lateness.total_seconds() // 60)))
                    self.log.info("skipped late %s schedule %s (%s late)", row["kind"], row["id"], lateness)
                    continue
                prefix = self._late_prefix(fire_at, now)

            if row["kind"] == "call":
                self._fire_call(row, fire_at_iso, next_iso, report)
                continue

            try:
                body = self._render(row)
                text = prefix + body
                provider_id = self.send(row["channel"], row["chat_id"], text)
            except Exception as exc:  # noqa: BLE001 - any provider/render failure is a failed delivery
                self.store.finish_delivery(row["id"], fire_at_iso, "failed", error=str(exc)[:500])
                report.failed.append(self._summary(row, error=str(exc)))
                self.log.warning("failed to send schedule %s: %s", row["id"], exc)
                continue
            self.store.finish_delivery(
                row["id"], fire_at_iso, "sent", provider_message_id=str(provider_id) if provider_id else None
            )
            report.sent.append(self._summary(row, text=text, provider_message_id=provider_id, next_run_utc=next_iso))

            priority = row.get("priority") or "normal"
            if priority == "critical":
                self._call_now(row, body, report)
            elif priority == "important":
                self._schedule_escalation(row, fire_at_iso, body, now)
        return report

    # ---------------------------------------------------------------- calls
    def _fire_call(self, row: dict[str, Any], fire_at_iso: str, next_iso: str | None, report: TickReport) -> None:
        """Ring the user's phone for a `call` row; never sends a text."""
        if not self.voice_ready():
            self.store.finish_delivery(row["id"], fire_at_iso, "failed", error=VOICE_NOT_CONFIGURED)
            report.failed.append(self._summary(row, error=VOICE_NOT_CONFIGURED))
            self.log.warning("schedule %s wants a call but %s", row["id"], VOICE_NOT_CONFIGURED)
            return
        try:
            text = self._render(row)
            call_id = self.place_call(self.config.user_phone, text)  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001 - provider failure is a failed delivery
            self.store.finish_delivery(row["id"], fire_at_iso, "failed", error=str(exc)[:500])
            report.failed.append(self._summary(row, error=str(exc)))
            self.log.warning("failed to place call for schedule %s: %s", row["id"], exc)
            return
        self.store.finish_delivery(
            row["id"], fire_at_iso, "sent", provider_message_id=str(call_id) if call_id else None
        )
        report.sent.append(self._summary(row, text=text, provider_message_id=call_id, next_run_utc=next_iso))
        self.log.info("called %s for schedule %s", self.config.user_phone, row["id"])

    def _call_now(self, row: dict[str, Any], text: str, report: TickReport) -> None:
        """A critical row was texted; also ring the phone right away."""
        if not self.voice_ready():
            self.log.warning("schedule %s is critical but %s; sent the text only", row["id"], VOICE_NOT_CONFIGURED)
            return
        try:
            call_id = self.place_call(self.config.user_phone, text)  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001 - the text went out; record the call separately
            report.failed.append(self._summary(row, via="call", error=str(exc)))
            self.log.warning("failed to place call for critical schedule %s: %s", row["id"], exc)
            return
        report.sent.append(self._summary(row, via="call", text=text, provider_message_id=call_id))
        self.log.info("called %s for critical schedule %s", self.config.user_phone, row["id"])

    def _schedule_escalation(self, row: dict[str, Any], fire_at_iso: str, text: str, now: datetime) -> None:
        """Queue a call for an important text; an inbound reply cancels it."""
        if not self.voice_ready():
            self.log.warning("schedule %s is important but %s; no follow-up call", row["id"], VOICE_NOT_CONFIGURED)
            return
        source = f"{ESCALATION_PREFIX}{row['id']}:{fire_at_iso}"
        due = now + timedelta(minutes=max(1, self.config.escalate_minutes))
        self.store.upsert_schedule_by_source(
            source,
            kind="call",
            text=text,
            channel=row["channel"],
            chat_id=row["chat_id"],
            next_run_utc=to_utc_iso(due),
            tz=row.get("tz") or self.config.timezone,
            priority="critical",
        )
        self.log.info("schedule %s is important; will call at %s unless the user replies", row["id"], to_utc_iso(due))

    # ------------------------------------------------------ auto reminders
    def _collect_task_reminders(self, now: datetime) -> dict[str, tuple[str, str, str]]:
        """source -> (fire_at_utc_iso, text, priority) for open tasks with a due time.

        High-priority tasks get an ``important`` reminder: a text, then a call
        if the user does not reply.
        """
        lead = self.config.task_lead_minutes
        out: dict[str, tuple[str, str, str]] = {}
        for task in self.store.list_tasks("open"):
            due = task.get("due") or ""
            if "T" not in due:
                continue
            try:
                due_dt = parse_local_datetime(due, self.tz)
            except ValueError:
                continue
            fire = due_dt - timedelta(minutes=lead)
            if fire < now - self.grace:
                continue
            when = "now" if lead <= 0 else f"in {lead} min"
            priority = "important" if task.get("priority") == "high" else "normal"
            out[f"task:{task['id']}"] = (to_utc_iso(fire), f"Reminder: {task['title']} is due {when}.", priority)
        return out

    def _event_text(self, title: str, start_local: datetime, location: str) -> str:
        lead = self.config.event_lead_minutes
        head = f"In {lead} min: " if lead > 0 else "Now: "
        text = f"{head}{title} ({start_local.strftime('%H:%M')})"
        if location:
            text += f" @ {location}"
        return text

    def _collect_event_reminders(self, now: datetime) -> dict[str, tuple[str, str, str]]:
        lead = timedelta(minutes=self.config.event_lead_minutes)
        now_local = now.astimezone(self.tz)
        window_start = (now_local - self.grace - lead).strftime("%Y-%m-%dT%H:%M")
        window_end = (now_local + GOOGLE_LOOKAHEAD).strftime("%Y-%m-%dT%H:%M")
        out: dict[str, tuple[str, str, str]] = {}
        for event in self.store.list_events(start=window_start, end=window_end, limit=200):
            start = event.get("start") or ""
            if "T" not in start:
                continue
            try:
                start_dt = parse_local_datetime(start, self.tz)
            except ValueError:
                continue
            fire = start_dt - lead
            if fire < now - self.grace:
                continue
            text = self._event_text(event["title"], start_dt.astimezone(self.tz), event.get("location") or "")
            out[f"event:{event['id']}"] = (to_utc_iso(fire), text, "normal")
        return out

    def _collect_google_reminders(self, now: datetime) -> dict[str, tuple[str, str, str]] | None:
        """None means Google could not be read, so existing gcal rows are left alone."""
        if self.google is None:
            return {}
        lead = timedelta(minutes=self.config.event_lead_minutes)
        now_local = now.astimezone(self.tz)
        try:
            events = self.google.list_events(
                start=now_local.strftime("%Y-%m-%dT%H:%M"),
                end=(now_local + GOOGLE_LOOKAHEAD).strftime("%Y-%m-%dT%H:%M"),
                limit=100,
            )
        except Exception as exc:  # noqa: BLE001 - network/API failure must not stop the daemon
            self.log.warning("could not read Google Calendar for reminders: %s", exc)
            return None
        out: dict[str, tuple[str, str, str]] = {}
        for event in events or []:
            start = event.get("start") or ""
            if event.get("all_day") or "T" not in start or not event.get("id"):
                continue
            try:
                start_dt = parse_local_datetime(start, self.tz)
            except ValueError:
                continue
            fire = start_dt - lead
            if fire < now - self.grace:
                continue
            title = event.get("title") or "(no title)"
            text = self._event_text(title, start_dt.astimezone(self.tz), event.get("location") or "")
            out[f"gcal:{event['id']}"] = (to_utc_iso(fire), text, "normal")
        return out

    def _reconcile(
        self, prefix: str, kind: str, live: dict[str, tuple[str, str, str]], target: tuple[str, str]
    ) -> None:
        channel, chat_id = target
        existing = {row["source"]: row for row in self.store.schedules_by_source_prefix(prefix)}
        for source, row in existing.items():
            if source not in live:
                self.store.delete_schedule(row["id"])
        for source, (fire_iso, text, priority) in live.items():
            row = existing.get(source)
            if row is not None and not row["enabled"] and row["next_run_utc"] == fire_iso:
                continue  # already sent for this fire time
            self.store.upsert_schedule_by_source(
                source,
                kind=kind,
                text=text,
                channel=channel,
                chat_id=chat_id,
                next_run_utc=fire_iso,
                tz=self.config.timezone,
                enabled=True,
                priority=priority,
            )

    def sync_auto_reminders(self, now: datetime | None = None) -> None:
        """Mirror tasks with due times and upcoming events into reminder rows."""
        target = self.config.default_target()
        if target is None:
            return
        now = (now or self._now()).astimezone(timezone.utc)
        self._reconcile("task:", "task_reminder", self._collect_task_reminders(now), target)
        self._reconcile("event:", "event_reminder", self._collect_event_reminders(now), target)
        gcal = self._collect_google_reminders(now)
        if gcal is not None:
            self._reconcile("gcal:", "event_reminder", gcal, target)

    # ------------------------------------------------------ fixed schedules
    def ensure_fixed_schedules(self) -> None:
        """Create/update the morning briefing and evening review rows from config.

        An empty time (or no messaging target) removes the row. A row the user
        cancelled with cancel_scheduled stays cancelled until the configured
        time changes.
        """
        target = self.config.default_target()
        now = self._now()
        fixed = {
            "system:morning": ("briefing", self.config.morning_briefing),
            "system:evening": ("review", self.config.evening_review),
        }
        for source, (kind, local_time) in fixed.items():
            existing = next(iter(self.store.schedules_by_source_prefix(source)), None)
            if not local_time or target is None:
                if existing is not None:
                    self.store.delete_schedule(existing["id"])
                continue
            channel, chat_id = target
            unchanged = (
                existing is not None
                and existing["local_time"] == local_time
                and existing["repeat"] == "daily"
                and existing["tz"] == self.config.timezone
                and existing["channel"] == channel
                and existing["chat_id"] == chat_id
            )
            if unchanged and not existing["enabled"]:
                continue
            if unchanged and from_utc_iso(existing["next_run_utc"]) >= now - self.grace:
                continue  # keep a pending (possibly catch-up) firing
            self.store.upsert_schedule_by_source(
                source,
                kind=kind,
                text="",
                channel=channel,
                chat_id=chat_id,
                next_run_utc=to_utc_iso(next_occurrence("daily", local_time, now, self.tz)),
                repeat="daily",
                local_time=local_time,
                tz=self.config.timezone,
                enabled=True,
            )

    # ----------------------------------------------------------- run loops
    def _recover_stale_deliveries(self) -> None:
        cutoff = to_utc_iso(self._now() - timedelta(minutes=STALE_SENDING_MINUTES))
        for d in self.store.stale_sending_deliveries(cutoff):
            self.log.warning(
                "delivery for schedule %s at %s was left in 'sending'; marking failed",
                d["schedule_id"], d["fire_at_utc"],
            )
            self.store.finish_delivery(
                d["schedule_id"], d["fire_at_utc"], "failed", error="interrupted before completion"
            )

    def run_once(self) -> TickReport:
        self.sync_auto_reminders()
        return self.tick()

    def run_forever(self, stop_event: threading.Event) -> None:
        self._recover_stale_deliveries()
        try:
            self.ensure_fixed_schedules()
        except Exception:  # noqa: BLE001
            self.log.exception("could not set up fixed schedules")
        n = 0
        while not stop_event.is_set():
            try:
                if n % SYNC_EVERY_TICKS == 0:
                    self.sync_auto_reminders()
                report = self.tick()
                if report.sent or report.failed:
                    self.log.info(
                        "tick: %d sent, %d failed, %d skipped",
                        len(report.sent), len(report.failed), len(report.skipped),
                    )
            except Exception:  # noqa: BLE001 - keep the daemon alive
                self.log.exception("scheduler tick failed")
            n += 1
            if stop_event.wait(max(1, self.config.tick_seconds)):
                break
