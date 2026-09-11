"""SQLite-backed storage for tasks, notes, events, memories, chat history, and schedules."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    due TEXT,
    priority TEXT NOT NULL DEFAULT 'normal',
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    start TEXT NOT NULL,
    end TEXT,
    location TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE TABLE IF NOT EXISTS scheduled_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,            -- custom | briefing | review | task_reminder | event_reminder
    text TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    next_run_utc TEXT NOT NULL,    -- aware ISO UTC
    repeat TEXT,                   -- NULL | daily | weekdays | weekends | weekly | days:mon,wed
    local_time TEXT,               -- HH:MM for recurring rows
    tz TEXT NOT NULL,
    source TEXT,                   -- NULL for user rows; system:morning | system:evening | task:<id> | event:<id> | gcal:<id>
    enabled INTEGER NOT NULL DEFAULT 1,
    last_sent_utc TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sched_source ON scheduled_messages(source) WHERE source IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sched_due ON scheduled_messages(enabled, next_run_utc);
CREATE TABLE IF NOT EXISTS deliveries (
    schedule_id INTEGER NOT NULL REFERENCES scheduled_messages(id) ON DELETE CASCADE,
    fire_at_utc TEXT NOT NULL,
    status TEXT NOT NULL,          -- sending | sent | failed | skipped
    provider_message_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (schedule_id, fire_at_utc)
);
CREATE TABLE IF NOT EXISTS channel_sessions (
    channel TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (channel, chat_id)
);
CREATE TABLE IF NOT EXISTS channel_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inbound_messages (
    channel TEXT NOT NULL,
    message_id TEXT NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY (channel, message_id)
);
"""

PRIORITIES = ("low", "normal", "high")
TASK_STATUSES = ("open", "done")
SCHEDULE_KINDS = ("custom", "briefing", "review", "task_reminder", "event_reminder")
DELIVERY_STATUSES = ("sending", "sent", "failed", "skipped")
AUTO_SOURCE_PREFIXES = ("task:", "event:", "gcal:")
MAX_SCHEDULE_TEXT = 1000


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Store:
    """Thin wrapper over sqlite3 with a small, explicit API."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            # WAL lets the daemon and an interactive chat share the file safely.
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ helpers
    def _rows(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

    def _row(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        row = self.conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None

    # -------------------------------------------------------------------- tasks
    def add_task(
        self,
        title: str,
        due: str | None = None,
        priority: str = "normal",
        notes: str = "",
    ) -> dict[str, Any]:
        title = title.strip()
        if not title:
            raise ValueError("task title cannot be empty")
        if priority not in PRIORITIES:
            raise ValueError(f"priority must be one of {PRIORITIES}")
        cur = self.conn.execute(
            "INSERT INTO tasks (title, notes, due, priority, status, created_at)"
            " VALUES (?, ?, ?, ?, 'open', ?)",
            (title, notes or "", due or None, priority, now_iso()),
        )
        self.conn.commit()
        return self.get_task(cur.lastrowid)  # type: ignore[arg-type]

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        return self._row("SELECT * FROM tasks WHERE id = ?", (task_id,))

    def list_tasks(
        self, status: str = "open", due_before: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM tasks"
        clauses: list[str] = []
        params: list[Any] = []
        if status != "all":
            clauses.append("status = ?")
            params.append(status)
        if due_before:
            clauses.append("due IS NOT NULL AND due <= ?")
            params.append(due_before)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += (
            " ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END,"
            " CASE WHEN due IS NULL THEN 1 ELSE 0 END, due,"
            " CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, id"
        )
        return self._rows(sql, params)

    def update_task(self, task_id: int, **fields: Any) -> dict[str, Any] | None:
        allowed = {"title", "notes", "due", "priority", "status"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if "priority" in updates and updates["priority"] not in PRIORITIES:
            raise ValueError(f"priority must be one of {PRIORITIES}")
        if "status" in updates and updates["status"] not in TASK_STATUSES:
            raise ValueError(f"status must be one of {TASK_STATUSES}")
        if "status" in updates:
            updates["completed_at"] = now_iso() if updates["status"] == "done" else None
        if not updates:
            return self.get_task(task_id)
        assignments = ", ".join(f"{k} = ?" for k in updates)
        self.conn.execute(
            f"UPDATE tasks SET {assignments} WHERE id = ?", (*updates.values(), task_id)
        )
        self.conn.commit()
        return self.get_task(task_id)

    def delete_task(self, task_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        self.conn.commit()
        return cur.rowcount > 0

    # -------------------------------------------------------------------- notes
    def add_note(self, title: str, body: str = "", tags: list[str] | None = None) -> dict[str, Any]:
        title = title.strip()
        if not title:
            raise ValueError("note title cannot be empty")
        ts = now_iso()
        cur = self.conn.execute(
            "INSERT INTO notes (title, body, tags, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (title, body or "", ",".join(t.strip() for t in (tags or []) if t.strip()), ts, ts),
        )
        self.conn.commit()
        return self.get_note(cur.lastrowid)  # type: ignore[arg-type]

    def get_note(self, note_id: int) -> dict[str, Any] | None:
        return self._row("SELECT * FROM notes WHERE id = ?", (note_id,))

    def update_note(self, note_id: int, **fields: Any) -> dict[str, Any] | None:
        allowed = {"title", "body", "tags"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return self.get_note(note_id)
        if "tags" in updates and isinstance(updates["tags"], list):
            updates["tags"] = ",".join(t.strip() for t in updates["tags"] if t.strip())
        updates["updated_at"] = now_iso()
        assignments = ", ".join(f"{k} = ?" for k in updates)
        self.conn.execute(
            f"UPDATE notes SET {assignments} WHERE id = ?", (*updates.values(), note_id)
        )
        self.conn.commit()
        return self.get_note(note_id)

    def search_notes(self, query: str = "", limit: int = 20) -> list[dict[str, Any]]:
        if not query.strip():
            return self._rows("SELECT * FROM notes ORDER BY updated_at DESC LIMIT ?", (limit,))
        like = f"%{query.strip()}%"
        return self._rows(
            "SELECT * FROM notes WHERE title LIKE ? OR body LIKE ? OR tags LIKE ?"
            " ORDER BY updated_at DESC LIMIT ?",
            (like, like, like, limit),
        )

    def delete_note(self, note_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        self.conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------- events
    def add_event(
        self,
        title: str,
        start: str,
        end: str | None = None,
        location: str = "",
        notes: str = "",
    ) -> dict[str, Any]:
        title = title.strip()
        if not title:
            raise ValueError("event title cannot be empty")
        if not start:
            raise ValueError("event start is required")
        cur = self.conn.execute(
            "INSERT INTO events (title, start, end, location, notes, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (title, start, end or None, location or "", notes or "", now_iso()),
        )
        self.conn.commit()
        return self.get_event(cur.lastrowid)  # type: ignore[arg-type]

    def get_event(self, event_id: int) -> dict[str, Any] | None:
        return self._row("SELECT * FROM events WHERE id = ?", (event_id,))

    def list_events(
        self, start: str | None = None, end: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events"
        clauses: list[str] = []
        params: list[Any] = []
        if start:
            clauses.append("start >= ?")
            params.append(start)
        if end:
            clauses.append("start <= ?")
            params.append(end)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY start LIMIT ?"
        params.append(limit)
        return self._rows(sql, params)

    def delete_event(self, event_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
        self.conn.commit()
        return cur.rowcount > 0

    # ----------------------------------------------------------------- memories
    def add_memory(self, content: str, category: str = "general") -> dict[str, Any]:
        content = content.strip()
        if not content:
            raise ValueError("memory content cannot be empty")
        cur = self.conn.execute(
            "INSERT INTO memories (content, category, created_at) VALUES (?, ?, ?)",
            (content, category or "general", now_iso()),
        )
        self.conn.commit()
        return self._row("SELECT * FROM memories WHERE id = ?", (cur.lastrowid,))  # type: ignore[return-value]

    def list_memories(self) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM memories ORDER BY id")

    def delete_memory(self, memory_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        self.conn.commit()
        return cur.rowcount > 0

    # ----------------------------------------------------------------- sessions
    def create_session(self, title: str = "") -> str:
        session_id = uuid.uuid4().hex[:12]
        ts = now_iso()
        self.conn.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (session_id, title, ts, ts),
        )
        self.conn.commit()
        return session_id

    def latest_session(self) -> str | None:
        """Most recent interactive session; chat-channel sessions are kept separate.

        Sessions created by `session_for_chat` are titled "<channel>:<chat_id>", so
        rotated (idle) chat sessions are excluded by title as well as by mapping.
        """
        row = self._row(
            "SELECT id FROM sessions WHERE id NOT IN (SELECT session_id FROM channel_sessions)"
            " AND title NOT IN (SELECT channel || ':' || chat_id FROM channel_sessions)"
            " ORDER BY updated_at DESC, rowid DESC LIMIT 1"
        )
        return row["id"] if row else None

    def list_sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT s.*, (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS message_count"
            " FROM sessions s ORDER BY updated_at DESC, rowid DESC LIMIT ?",
            (limit,),
        )

    def append_message(self, session_id: str, role: str, content: Any) -> None:
        ts = now_iso()
        self.conn.execute(
            "INSERT INTO messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (session_id, role, json.dumps(content, ensure_ascii=False), ts),
        )
        self.conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (ts, session_id))
        self.conn.commit()

    def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        rows = self._rows(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id", (session_id,)
        )
        return [{"role": r["role"], "content": json.loads(r["content"])} for r in rows]

    def delete_session(self, session_id: str) -> bool:
        cur = self.conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        self.conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------- schedules
    @staticmethod
    def _check_schedule_fields(kind: str | None, text: str | None, channel: str | None, chat_id: str | None) -> None:
        if kind is not None and kind not in SCHEDULE_KINDS:
            raise ValueError(f"kind must be one of {SCHEDULE_KINDS}")
        if text is not None and len(text) > MAX_SCHEDULE_TEXT:
            raise ValueError(f"text is too long (max {MAX_SCHEDULE_TEXT} characters)")
        if channel is not None and not channel.strip():
            raise ValueError("channel cannot be empty")
        if chat_id is not None and not str(chat_id).strip():
            raise ValueError("chat_id cannot be empty")

    def add_schedule(
        self,
        kind: str,
        channel: str,
        chat_id: str,
        next_run_utc: str,
        tz: str,
        text: str = "",
        repeat: str | None = None,
        local_time: str | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        self._check_schedule_fields(kind, text, channel, chat_id)
        if not next_run_utc:
            raise ValueError("next_run_utc is required")
        if not tz:
            raise ValueError("tz is required")
        cur = self.conn.execute(
            "INSERT INTO scheduled_messages"
            " (kind, text, channel, chat_id, next_run_utc, repeat, local_time, tz, source, enabled, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (kind, text or "", channel, str(chat_id), next_run_utc, repeat, local_time, tz, source, now_iso()),
        )
        self.conn.commit()
        return self.get_schedule(cur.lastrowid)  # type: ignore[return-value]

    def upsert_schedule_by_source(self, source: str, **fields: Any) -> dict[str, Any]:
        """Insert or update the single row identified by `source`.

        Required on insert: kind, channel, chat_id, next_run_utc, tz. On conflict
        text/next_run_utc/local_time/repeat/channel/chat_id/enabled are updated.
        """
        if not source:
            raise ValueError("source is required")
        allowed = {"kind", "text", "channel", "chat_id", "next_run_utc", "repeat", "local_time", "tz", "enabled"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown schedule field(s): {', '.join(sorted(unknown))}")
        for required in ("kind", "channel", "chat_id", "next_run_utc", "tz"):
            if not fields.get(required):
                raise ValueError(f"{required} is required")
        self._check_schedule_fields(fields["kind"], fields.get("text"), fields["channel"], fields["chat_id"])
        text = fields.get("text") or ""
        enabled = 1 if fields.get("enabled", True) else 0
        self.conn.execute(
            "INSERT INTO scheduled_messages"
            " (kind, text, channel, chat_id, next_run_utc, repeat, local_time, tz, source, enabled, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(source) WHERE source IS NOT NULL DO UPDATE SET"
            " text = excluded.text, next_run_utc = excluded.next_run_utc,"
            " local_time = excluded.local_time, repeat = excluded.repeat,"
            " channel = excluded.channel, chat_id = excluded.chat_id, enabled = excluded.enabled",
            (
                fields["kind"],
                text,
                fields["channel"],
                str(fields["chat_id"]),
                fields["next_run_utc"],
                fields.get("repeat"),
                fields.get("local_time"),
                fields["tz"],
                source,
                enabled,
                now_iso(),
            ),
        )
        self.conn.commit()
        return self._row("SELECT * FROM scheduled_messages WHERE source = ?", (source,))  # type: ignore[return-value]

    def get_schedule(self, schedule_id: int) -> dict[str, Any] | None:
        return self._row("SELECT * FROM scheduled_messages WHERE id = ?", (schedule_id,))

    def list_schedules(
        self,
        enabled_only: bool = True,
        channel: str | None = None,
        chat_id: str | None = None,
        include_system: bool = False,
    ) -> list[dict[str, Any]]:
        """List schedules. `include_system=False` hides the auto-generated task:/event:/gcal:
        reminder rows but keeps the fixed system:* rows and user-created rows."""
        clauses: list[str] = []
        params: list[Any] = []
        if enabled_only:
            clauses.append("enabled = 1")
        if channel:
            clauses.append("channel = ?")
            params.append(channel)
        if chat_id:
            clauses.append("chat_id = ?")
            params.append(str(chat_id))
        if not include_system:
            for prefix in AUTO_SOURCE_PREFIXES:
                clauses.append("(source IS NULL OR source NOT LIKE ?)")
                params.append(f"{prefix}%")
        sql = "SELECT * FROM scheduled_messages"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY next_run_utc, id"
        return self._rows(sql, params)

    def cancel_schedule(self, schedule_id: int) -> bool:
        cur = self.conn.execute(
            "UPDATE scheduled_messages SET enabled = 0 WHERE id = ? AND enabled = 1", (schedule_id,)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def delete_schedule(self, schedule_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM scheduled_messages WHERE id = ?", (schedule_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def schedules_by_source_prefix(self, prefix: str) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM scheduled_messages WHERE source LIKE ? ORDER BY id", (f"{prefix}%",)
        )

    def due_schedules(self, now_utc_iso: str) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM scheduled_messages WHERE enabled = 1 AND next_run_utc <= ?"
            " ORDER BY next_run_utc, id",
            (now_utc_iso,),
        )

    def advance_schedule(
        self, schedule_id: int, next_run_utc: str | None, last_sent_utc: str
    ) -> dict[str, Any] | None:
        """Move a row to its next firing time; None disables it (one-shot done)."""
        if next_run_utc is None:
            self.conn.execute(
                "UPDATE scheduled_messages SET enabled = 0, last_sent_utc = ? WHERE id = ?",
                (last_sent_utc, schedule_id),
            )
        else:
            self.conn.execute(
                "UPDATE scheduled_messages SET next_run_utc = ?, last_sent_utc = ? WHERE id = ?",
                (next_run_utc, last_sent_utc, schedule_id),
            )
        self.conn.commit()
        return self.get_schedule(schedule_id)

    # ------------------------------------------------------------ deliveries
    def claim_delivery(self, schedule_id: int, fire_at_utc: str) -> bool:
        """Atomically reserve one (schedule, fire time) so a message is sent at most once."""
        try:
            self.conn.execute(
                "INSERT INTO deliveries (schedule_id, fire_at_utc, status, created_at)"
                " VALUES (?, ?, 'sending', ?)",
                (schedule_id, fire_at_utc, now_iso()),
            )
        except sqlite3.IntegrityError:
            self.conn.rollback()
            return False
        self.conn.commit()
        return True

    def finish_delivery(
        self,
        schedule_id: int,
        fire_at_utc: str,
        status: str,
        provider_message_id: str | None = None,
        error: str | None = None,
    ) -> None:
        if status not in DELIVERY_STATUSES:
            raise ValueError(f"status must be one of {DELIVERY_STATUSES}")
        self.conn.execute(
            "UPDATE deliveries SET status = ?, provider_message_id = ?, error = ?"
            " WHERE schedule_id = ? AND fire_at_utc = ?",
            (status, provider_message_id, error, schedule_id, fire_at_utc),
        )
        self.conn.commit()

    def list_deliveries(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT d.*, s.kind, s.channel, s.chat_id, s.source FROM deliveries d"
            " LEFT JOIN scheduled_messages s ON s.id = d.schedule_id"
            " ORDER BY d.created_at DESC, d.rowid DESC LIMIT ?",
            (limit,),
        )

    def stale_sending_deliveries(self, older_than_utc_iso: str) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM deliveries WHERE status = 'sending' AND created_at < ?",
            (older_than_utc_iso,),
        )

    # ------------------------------------------------------ channel sessions
    def session_for_chat(self, channel: str, chat_id: str, max_idle_seconds: int) -> str:
        """Session backing a chat; a new one starts when none exists or it went idle."""
        chat_id = str(chat_id)
        row = self._row(
            "SELECT cs.session_id, s.updated_at FROM channel_sessions cs"
            " JOIN sessions s ON s.id = cs.session_id WHERE cs.channel = ? AND cs.chat_id = ?",
            (channel, chat_id),
        )
        now = datetime.now(timezone.utc)
        if row is not None:
            try:
                updated = datetime.fromisoformat(row["updated_at"])
            except ValueError:
                updated = now
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            if (now - updated).total_seconds() <= max_idle_seconds:
                return row["session_id"]
        session_id = self.create_session(f"{channel}:{chat_id}")
        self.conn.execute(
            "INSERT INTO channel_sessions (channel, chat_id, session_id, updated_at)"
            " VALUES (?, ?, ?, ?) ON CONFLICT(channel, chat_id) DO UPDATE SET"
            " session_id = excluded.session_id, updated_at = excluded.updated_at",
            (channel, chat_id, session_id, now_iso()),
        )
        self.conn.commit()
        return session_id

    def reset_chat_session(self, channel: str, chat_id: str) -> str:
        """Start a fresh conversation for a chat and return the new session id."""
        return self.session_for_chat(channel, chat_id, max_idle_seconds=-1)

    # ---------------------------------------------------------- channel state
    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self._row("SELECT value FROM channel_state WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO channel_state (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        self.conn.commit()

    def record_inbound(self, channel: str, message_id: str) -> bool:
        """Remember an inbound message id; False when it was already seen (duplicate)."""
        try:
            self.conn.execute(
                "INSERT INTO inbound_messages (channel, message_id, received_at) VALUES (?, ?, ?)",
                (channel, str(message_id), now_iso()),
            )
        except sqlite3.IntegrityError:
            self.conn.rollback()
            return False
        self.conn.commit()
        return True
