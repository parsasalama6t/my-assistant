from __future__ import annotations

import pytest

from assistant.store import Store

TZ = "America/Toronto"


def _add(store: Store, **kw) -> dict:
    base = dict(kind="custom", channel="telegram", chat_id="42", next_run_utc="2026-09-12T12:00:00+00:00", tz=TZ, text="hi")
    base.update(kw)
    return store.add_schedule(**base)


def test_add_get_list_cancel_delete(store: Store) -> None:
    row = _add(store)
    assert row["enabled"] == 1 and row["source"] is None and row["repeat"] is None
    assert store.get_schedule(row["id"]) == row
    assert [r["id"] for r in store.list_schedules()] == [row["id"]]
    assert store.cancel_schedule(row["id"]) is True
    assert store.cancel_schedule(row["id"]) is False
    assert store.list_schedules() == []
    assert len(store.list_schedules(enabled_only=False)) == 1
    assert store.delete_schedule(row["id"]) is True
    assert store.get_schedule(row["id"]) is None


def test_add_validation(store: Store) -> None:
    with pytest.raises(ValueError):
        _add(store, kind="weird")
    with pytest.raises(ValueError):
        _add(store, text="x" * 1001)
    with pytest.raises(ValueError):
        _add(store, channel="")
    with pytest.raises(ValueError):
        _add(store, next_run_utc="")
    with pytest.raises(ValueError):
        _add(store, tz="")


def test_list_filters_and_system_visibility(store: Store) -> None:
    user = _add(store)
    morning = _add(store, kind="briefing", source="system:morning", repeat="daily", local_time="07:30")
    task = _add(store, kind="task_reminder", source="task:1")
    _add(store, kind="event_reminder", source="event:2")
    _add(store, kind="event_reminder", source="gcal:abc")
    other_chat = _add(store, chat_id="99")

    ids = {r["id"] for r in store.list_schedules()}
    assert ids == {user["id"], morning["id"], other_chat["id"]}
    assert len(store.list_schedules(include_system=True)) == 6
    assert [r["id"] for r in store.list_schedules(channel="telegram", chat_id="99")] == [other_chat["id"]]
    assert {r["id"] for r in store.schedules_by_source_prefix("task:")} == {task["id"]}
    assert len(store.schedules_by_source_prefix("system:")) == 1


def test_upsert_by_source(store: Store) -> None:
    first = store.upsert_schedule_by_source(
        "task:7", kind="task_reminder", text="a", channel="telegram", chat_id="1",
        next_run_utc="2026-09-12T12:00:00+00:00", tz=TZ,
    )
    second = store.upsert_schedule_by_source(
        "task:7", kind="task_reminder", text="b", channel="twilio", chat_id="+1",
        next_run_utc="2026-09-13T12:00:00+00:00", tz=TZ, enabled=False,
    )
    assert first["id"] == second["id"]
    assert second["text"] == "b" and second["channel"] == "twilio" and second["enabled"] == 0
    assert second["next_run_utc"] == "2026-09-13T12:00:00+00:00"
    assert len(store.list_schedules(enabled_only=False, include_system=True)) == 1
    with pytest.raises(ValueError):
        store.upsert_schedule_by_source("", kind="custom", channel="c", chat_id="1", next_run_utc="x", tz=TZ)
    with pytest.raises(ValueError):
        store.upsert_schedule_by_source("s", kind="custom", channel="c", chat_id="1", tz=TZ)
    with pytest.raises(ValueError):
        store.upsert_schedule_by_source("s", kind="custom", channel="c", chat_id="1", next_run_utc="x", tz=TZ, bogus=1)


def test_due_and_advance(store: Store) -> None:
    early = _add(store, next_run_utc="2026-09-12T12:00:00+00:00")
    late = _add(store, next_run_utc="2026-09-12T13:00:00+00:00")
    disabled = _add(store, next_run_utc="2026-09-12T11:00:00+00:00")
    store.cancel_schedule(disabled["id"])
    due = store.due_schedules("2026-09-12T12:30:00+00:00")
    assert [r["id"] for r in due] == [early["id"]]
    assert [r["id"] for r in store.due_schedules("2026-09-12T13:00:00+00:00")] == [early["id"], late["id"]]

    moved = store.advance_schedule(early["id"], "2026-09-13T12:00:00+00:00", "2026-09-12T12:30:00+00:00")
    assert moved["next_run_utc"] == "2026-09-13T12:00:00+00:00" and moved["last_sent_utc"] == "2026-09-12T12:30:00+00:00"
    assert moved["enabled"] == 1
    done = store.advance_schedule(late["id"], None, "2026-09-12T13:00:00+00:00")
    assert done["enabled"] == 0 and done["last_sent_utc"]
    assert store.due_schedules("2026-09-12T13:00:00+00:00") == []


def test_delivery_claim_is_once_only(store: Store) -> None:
    row = _add(store)
    assert store.claim_delivery(row["id"], row["next_run_utc"]) is True
    assert store.claim_delivery(row["id"], row["next_run_utc"]) is False
    assert store.claim_delivery(row["id"], "2026-09-13T12:00:00+00:00") is True
    store.finish_delivery(row["id"], row["next_run_utc"], "sent", provider_message_id="m1")
    store.finish_delivery(row["id"], "2026-09-13T12:00:00+00:00", "failed", error="boom")
    deliveries = store.list_deliveries()
    assert len(deliveries) == 2
    by_time = {d["fire_at_utc"]: d for d in deliveries}
    assert by_time[row["next_run_utc"]]["status"] == "sent"
    assert by_time[row["next_run_utc"]]["provider_message_id"] == "m1"
    assert by_time["2026-09-13T12:00:00+00:00"]["error"] == "boom"
    assert by_time[row["next_run_utc"]]["kind"] == "custom"
    with pytest.raises(ValueError):
        store.finish_delivery(row["id"], row["next_run_utc"], "maybe")
    # Deleting the schedule cascades to its deliveries.
    store.delete_schedule(row["id"])
    assert store.list_deliveries() == []


def test_stale_sending_deliveries(store: Store) -> None:
    row = _add(store)
    store.claim_delivery(row["id"], row["next_run_utc"])
    assert store.stale_sending_deliveries("2000-01-01T00:00:00+00:00") == []
    assert len(store.stale_sending_deliveries("2999-01-01T00:00:00+00:00")) == 1


def test_channel_sessions(store: Store) -> None:
    sid = store.session_for_chat("telegram", "42", max_idle_seconds=3600)
    assert store.session_for_chat("telegram", "42", max_idle_seconds=3600) == sid
    assert store.session_for_chat("telegram", "43", max_idle_seconds=3600) != sid
    store.append_message(sid, "user", "hi")
    # Idle limit of 0 seconds forces a fresh session (updated_at is at most now).
    fresh = store.session_for_chat("telegram", "42", max_idle_seconds=-1)
    assert fresh != sid
    assert store.get_messages(fresh) == []
    assert store.reset_chat_session("telegram", "42") not in {sid, fresh}
    titles = {s["title"] for s in store.list_sessions()}
    assert "telegram:42" in titles
    # Channel sessions never become the CLI's "latest" session.
    assert store.latest_session() is None
    cli = store.create_session("cli")
    assert store.latest_session() == cli


def test_state_and_inbound_dedup(store: Store) -> None:
    assert store.get_state("telegram:offset") is None
    assert store.get_state("telegram:offset", "0") == "0"
    store.set_state("telegram:offset", "17")
    store.set_state("telegram:offset", "18")
    assert store.get_state("telegram:offset") == "18"
    assert store.record_inbound("telegram", "m1") is True
    assert store.record_inbound("telegram", "m1") is False
    assert store.record_inbound("twilio", "m1") is True


def test_wal_mode_on_disk(tmp_path) -> None:
    s = Store(tmp_path / "assistant.db")
    assert s.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert s.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    s.close()
