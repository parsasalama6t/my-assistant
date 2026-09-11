from __future__ import annotations

import pytest

from assistant.store import Store


def test_task_lifecycle(store: Store) -> None:
    t = store.add_task("Buy milk", due="2026-09-11", priority="high")
    assert t["status"] == "open" and t["priority"] == "high"
    assert [x["id"] for x in store.list_tasks()] == [t["id"]]

    done = store.update_task(t["id"], status="done")
    assert done["status"] == "done" and done["completed_at"]
    assert store.list_tasks("open") == []
    assert len(store.list_tasks("all")) == 1

    assert store.delete_task(t["id"]) is True
    assert store.delete_task(t["id"]) is False


def test_task_ordering_and_due_filter(store: Store) -> None:
    store.add_task("no due")
    later = store.add_task("later", due="2026-09-20")
    soon = store.add_task("soon", due="2026-09-12")
    ids = [t["id"] for t in store.list_tasks()]
    assert ids[:2] == [soon["id"], later["id"]]
    assert [t["title"] for t in store.list_tasks(due_before="2026-09-15")] == ["soon"]


def test_task_validation(store: Store) -> None:
    with pytest.raises(ValueError):
        store.add_task("   ")
    with pytest.raises(ValueError):
        store.add_task("x", priority="urgent")
    with pytest.raises(ValueError):
        store.update_task(1, status="maybe")


def test_notes_search(store: Store) -> None:
    n = store.add_note("Gift ideas", "A record player for Dana", ["personal", "gifts"])
    assert n["tags"] == "personal,gifts"
    store.add_note("Work", "Quarterly plan")
    assert [x["title"] for x in store.search_notes("dana")] == ["Gift ideas"]
    assert [x["title"] for x in store.search_notes("gifts")] == ["Gift ideas"]
    assert len(store.search_notes("")) == 2
    updated = store.update_note(n["id"], body="A turntable", tags=["gifts"])
    assert updated["body"] == "A turntable" and updated["tags"] == "gifts"
    assert store.delete_note(n["id"])


def test_events_range(store: Store) -> None:
    store.add_event("Dentist", "2026-09-12T09:00", location="Downtown")
    store.add_event("Standup", "2026-09-11T10:00", end="2026-09-11T10:15")
    store.add_event("Trip", "2026-10-01")
    assert [e["title"] for e in store.list_events()] == ["Standup", "Dentist", "Trip"]
    assert [e["title"] for e in store.list_events("2026-09-12", "2026-09-30")] == ["Dentist"]
    with pytest.raises(ValueError):
        store.add_event("bad", "")


def test_memories(store: Store) -> None:
    m = store.add_memory("Prefers tea over coffee", "preference")
    assert store.list_memories()[0]["content"] == "Prefers tea over coffee"
    assert store.delete_memory(m["id"])
    assert store.list_memories() == []


def test_sessions_and_messages(store: Store) -> None:
    assert store.latest_session() is None
    first = store.create_session("a")
    store.append_message(first, "user", "hello")
    store.append_message(first, "assistant", [{"type": "text", "text": "hi"}])
    assert store.get_messages(first) == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
    ]
    second = store.create_session("b")
    store.append_message(second, "user", "x")
    assert store.latest_session() == second
    assert store.list_sessions()[0]["message_count"] == 1
    assert store.delete_session(first)
    assert store.get_messages(first) == []


def test_persistence_on_disk(tmp_path) -> None:
    path = tmp_path / "nested" / "assistant.db"
    s = Store(path)
    s.add_task("persisted")
    s.close()
    s2 = Store(path)
    assert s2.list_tasks()[0]["title"] == "persisted"
    s2.close()
