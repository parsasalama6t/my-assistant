"""SQLite-backed storage for tasks, notes, events, memories, and chat history."""

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
"""

PRIORITIES = ("low", "normal", "high")
TASK_STATUSES = ("open", "done")


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
        row = self._row("SELECT id FROM sessions ORDER BY updated_at DESC, rowid DESC LIMIT 1")
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
