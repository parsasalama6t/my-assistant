from __future__ import annotations

import json

from assistant.store import Store
from assistant.tools import GOOGLE_TOOLS, TOOLS, ToolContext, run_tool, tool_definitions


def test_definitions_are_well_formed() -> None:
    defs = tool_definitions()
    names = [d["name"] for d in defs]
    assert len(names) == len(set(names))
    for d in defs:
        assert d["description"]
        assert d["input_schema"]["type"] == "object"
    assert tool_definitions(web_search=True)[-1]["name"] == "web_search"
    assert len(defs) == len(TOOLS)
    with_google = tool_definitions(google=True)
    assert len(with_google) == len(TOOLS) + len(GOOGLE_TOOLS)
    assert "gcal_list_events" in {d["name"] for d in with_google}
    assert "gcal_list_events" not in {d["name"] for d in defs}


def test_task_roundtrip_through_tools(store: Store) -> None:
    out, err = run_tool(ToolContext(store), "add_task", {"title": "Call mom", "due": "2026-09-12"})
    assert not err
    task = json.loads(out)
    out, err = run_tool(ToolContext(store), "list_tasks", {})
    assert json.loads(out)["count"] == 1
    out, err = run_tool(ToolContext(store), "update_task", {"id": task["id"], "status": "done"})
    assert json.loads(out)["status"] == "done"
    out, err = run_tool(ToolContext(store), "list_tasks", {"status": "open"})
    assert json.loads(out)["count"] == 0


def test_errors_are_reported_not_raised(store: Store) -> None:
    out, err = run_tool(ToolContext(store), "delete_task", {"id": 999})
    assert err and "no task" in out
    out, err = run_tool(ToolContext(store), "add_task", {})
    assert err and "title" in out
    out, err = run_tool(ToolContext(store), "nope", {})
    assert err


def test_notes_events_memories(store: Store) -> None:
    out, _ = run_tool(ToolContext(store), "add_note", {"title": "Idea", "body": "Build a birdhouse", "tags": ["diy"]})
    note = json.loads(out)
    out, _ = run_tool(ToolContext(store), "search_notes", {"query": "birdhouse"})
    assert json.loads(out)["notes"][0]["id"] == note["id"]

    out, _ = run_tool(ToolContext(store), "add_event", {"title": "Lunch", "start": "2026-09-12T12:00"})
    out, _ = run_tool(ToolContext(store), "list_events", {"start": "2026-09-12", "end": "2026-09-13"})
    assert json.loads(out)["count"] == 1

    out, _ = run_tool(ToolContext(store), "remember", {"content": "Vegetarian", "category": "preference"})
    mem = json.loads(out)
    out, _ = run_tool(ToolContext(store), "list_memories", {})
    assert json.loads(out)["count"] == 1
    out, err = run_tool(ToolContext(store), "forget", {"id": mem["id"]})
    assert not err


def test_current_datetime(store: Store) -> None:
    out, err = run_tool(ToolContext(store), "get_current_datetime", {})
    data = json.loads(out)
    assert not err and data["weekday"] and "T" in data["datetime"]


def test_google_tools_error_when_not_connected(store: Store) -> None:
    out, err = run_tool(ToolContext(store), "gcal_list_events", {})
    assert err and "google login" in out


# ------------------------------------------------------------ scheduling
from datetime import datetime, timedelta, timezone  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from assistant.config import Config  # noqa: E402
from assistant.tools import SCHEDULE_TOOLS  # noqa: E402

TORONTO = ZoneInfo("America/Toronto")


def _messaging_config() -> Config:
    return Config(timezone="America/Toronto", default_channel="telegram", telegram_bot_token="t", telegram_chat_ids=["42", "43"])


def test_scheduling_flag_adds_three_tools_and_default_unchanged() -> None:
    assert len(SCHEDULE_TOOLS) == 3
    assert len(tool_definitions()) == len(TOOLS)
    names = {d["name"] for d in tool_definitions(scheduling=True)}
    assert {"schedule_message", "list_scheduled", "cancel_scheduled"} <= names
    assert len(tool_definitions(scheduling=True)) == len(TOOLS) + 3
    assert "schedule_message" not in {d["name"] for d in tool_definitions()}
    assert len(tool_definitions(google=True, scheduling=True, web_search=True)) == len(TOOLS) + len(GOOGLE_TOOLS) + 4


def test_schedule_message_without_target_errors(store: Store) -> None:
    out, err = run_tool(ToolContext(store), "schedule_message", {"text": "hi", "when": "2999-01-01T10:00"})
    assert err and "No messaging channel" in out
    out, err = run_tool(
        ToolContext(store, config=Config(timezone="America/Toronto")),
        "schedule_message", {"text": "hi", "when": "2999-01-01T10:00"},
    )
    assert err and "No messaging channel" in out
    assert store.list_schedules(include_system=True) == []


def test_schedule_round_trip_with_chat_context(store: Store) -> None:
    ctx = ToolContext(store, tz=TORONTO, channel="telegram", chat_id="43", config=_messaging_config())
    out, err = run_tool(ctx, "schedule_message", {"text": "Leave for the gym", "when": "2999-07-01T07:30"})
    assert not err, out
    created = json.loads(out)
    assert created["next_run"] == "2999-07-01 07:30" and created["repeat"] is None
    row = store.get_schedule(created["id"])
    assert row["channel"] == "telegram" and row["chat_id"] == "43" and row["tz"] == "America/Toronto"
    assert row["next_run_utc"] == "2999-07-01T11:30:00+00:00"  # 07:30 EDT

    out, err = run_tool(ctx, "list_scheduled", {})
    listed = json.loads(out)
    assert listed["count"] == 1 and listed["timezone"] == "America/Toronto"
    assert listed["scheduled"][0]["id"] == created["id"] and listed["scheduled"][0]["text"] == "Leave for the gym"

    out, err = run_tool(ctx, "cancel_scheduled", {"id": created["id"]})
    assert not err and json.loads(out)["cancelled"] == created["id"]
    assert store.list_schedules() == []
    out, err = run_tool(ctx, "cancel_scheduled", {"id": created["id"]})
    assert err


def test_schedule_message_uses_default_target_and_repeat(store: Store) -> None:
    ctx = ToolContext(store, tz=TORONTO, config=_messaging_config())
    # 2999-09-14 is a Saturday: a weekdays rule rolls the first send to Monday the 16th.
    assert datetime(2999, 9, 14).weekday() == 5
    out, err = run_tool(ctx, "schedule_message", {"text": "Water", "when": "2999-09-14T08:00", "repeat": "weekdays"})
    assert not err, out
    created = json.loads(out)
    row = store.get_schedule(created["id"])
    assert row["chat_id"] == "42" and row["repeat"] == "weekdays" and row["local_time"] == "08:00"
    assert created["next_run"] == "2999-09-16 08:00"
    # A weekday `when` is kept as-is.
    out, err = run_tool(ctx, "schedule_message", {"text": "Water", "when": "2999-09-16T08:00", "repeat": "weekdays"})
    assert not err and json.loads(out)["next_run"] == "2999-09-16 08:00"
    # A past `when` with a repeat rolls forward to the next occurrence instead of erroring.
    out, err = run_tool(ctx, "schedule_message", {"text": "Morning", "when": "2020-01-01T08:00", "repeat": "daily"})
    assert not err, out
    nxt = datetime.fromisoformat(store.get_schedule(json.loads(out)["id"])["next_run_utc"])
    assert nxt > datetime.now(timezone.utc) and nxt - datetime.now(timezone.utc) <= timedelta(days=1)
    # weekly stores the weekday of the first send.
    out, err = run_tool(ctx, "schedule_message", {"text": "Weekly", "when": "2999-09-14T08:00", "repeat": "weekly"})
    assert not err and store.get_schedule(json.loads(out)["id"])["repeat"] == "days:sat"


def test_schedule_message_validation(store: Store) -> None:
    ctx = ToolContext(store, tz=TORONTO, config=_messaging_config())
    out, err = run_tool(ctx, "schedule_message", {"text": "  ", "when": "2999-01-01T10:00"})
    assert err and "text" in out
    out, err = run_tool(ctx, "schedule_message", {"text": "x", "when": "2020-01-01T10:00"})
    assert err and "past" in out
    out, err = run_tool(ctx, "schedule_message", {"text": "x", "when": "soonish"})
    assert err
    out, err = run_tool(ctx, "schedule_message", {"text": "x", "when": "2999-01-01T10:00", "repeat": "hourly"})
    assert err and "repeat" in out.lower()
    # A chat id outside the allowlist is refused.
    bad = ToolContext(store, tz=TORONTO, channel="telegram", chat_id="999", config=_messaging_config())
    out, err = run_tool(bad, "schedule_message", {"text": "x", "when": "2999-01-01T10:00"})
    assert err and "not an allowed" in out


def test_cancel_refuses_auto_rows_but_allows_system(store: Store) -> None:
    ctx = ToolContext(store, tz=TORONTO, config=_messaging_config())
    auto = store.add_schedule("task_reminder", "telegram", "42", "2999-01-01T10:00:00+00:00", "America/Toronto", text="t", source="task:1")
    gcal = store.add_schedule("event_reminder", "telegram", "42", "2999-01-01T10:00:00+00:00", "America/Toronto", text="g", source="gcal:abc")
    system = store.add_schedule("briefing", "telegram", "42", "2999-01-01T10:00:00+00:00", "America/Toronto", repeat="daily", local_time="07:30", source="system:morning")
    for row in (auto, gcal):
        out, err = run_tool(ctx, "cancel_scheduled", {"id": row["id"]})
        assert err and "automatically" in out
        assert store.get_schedule(row["id"])["enabled"] == 1
    out, err = run_tool(ctx, "cancel_scheduled", {"id": system["id"]})
    assert not err and store.get_schedule(system["id"])["enabled"] == 0
    # list_scheduled hides auto rows but shows system rows.
    out, _ = run_tool(ctx, "list_scheduled", {})
    assert json.loads(out)["count"] == 0
    store.upsert_schedule_by_source("system:morning", kind="briefing", channel="telegram", chat_id="42", next_run_utc="2999-01-01T10:00:00+00:00", tz="America/Toronto", enabled=True)
    out, _ = run_tool(ctx, "list_scheduled", {})
    assert [s["source"] for s in json.loads(out)["scheduled"]] == ["system:morning"]


def test_current_datetime_uses_context_timezone(store: Store) -> None:
    out, _ = run_tool(ToolContext(store, tz=TORONTO), "get_current_datetime", {})
    data = json.loads(out)
    assert data["timezone"] == "America/Toronto"
    assert data["datetime"].endswith(("-04:00", "-05:00"))
    out, _ = run_tool(ToolContext(store), "get_current_datetime", {})
    assert json.loads(out)["datetime"].endswith("+00:00")


# ----------------------------------------------------------------- voice
from assistant.tools import VOICE_TOOLS  # noqa: E402


def _voice_config() -> Config:
    return Config(
        timezone="America/Toronto", default_channel="twilio", twilio_account_sid="AC1",
        twilio_auth_token="tok", twilio_from="+15550001111", user_phone="+14165550100",
    )


def test_voice_flag_adds_call_me() -> None:
    assert [t.name for t in VOICE_TOOLS] == ["call_me"]
    assert "call_me" not in {d["name"] for d in tool_definitions()}
    assert "call_me" not in {d["name"] for d in tool_definitions(scheduling=True)}
    with_voice = tool_definitions(voice=True)
    assert len(with_voice) == len(TOOLS) + 1 and with_voice[-1]["name"] == "call_me"
    everything = tool_definitions(google=True, scheduling=True, voice=True, web_search=True)
    assert len(everything) == len(TOOLS) + len(GOOGLE_TOOLS) + 3 + 1 + 1
    assert everything[-1]["name"] == "web_search" and everything[-2]["name"] == "call_me"


def test_schedule_message_priority(store: Store) -> None:
    ctx = ToolContext(store, tz=TORONTO, config=_messaging_config())
    out, err = run_tool(ctx, "schedule_message", {"text": "x", "when": "2999-01-01T10:00", "priority": "urgent"})
    assert err and "priority" in out and store.list_schedules() == []
    out, err = run_tool(ctx, "schedule_message", {"text": "x", "when": "2999-01-01T10:00", "priority": "important"})
    assert not err, out
    view = json.loads(out)
    assert view["priority"] == "important" and store.get_schedule(view["id"])["priority"] == "important"
    assert "text only" in view["note"]  # no voice in this config
    out, err = run_tool(ctx, "schedule_message", {"text": "y", "when": "2999-01-01T10:00"})
    assert not err and json.loads(out)["priority"] == "normal" and "note" not in json.loads(out)
    voiced = ToolContext(store, tz=TORONTO, config=_voice_config(), place_call=lambda n, t: "CA1")
    out, err = run_tool(voiced, "schedule_message", {"text": "z", "when": "2999-01-01T10:00", "priority": "CRITICAL"})
    assert not err and json.loads(out)["priority"] == "critical" and "note" not in json.loads(out)
    schema = next(d for d in tool_definitions(scheduling=True) if d["name"] == "schedule_message")
    assert schema["input_schema"]["properties"]["priority"]["enum"] == ["normal", "important", "critical"]


def test_call_me_uses_place_call_or_errors(store: Store) -> None:
    calls: list[tuple[str, str]] = []

    def place_call(to_number: str, text: str) -> str:
        calls.append((to_number, text))
        return "CA42"

    ctx = ToolContext(store, config=_voice_config(), place_call=place_call)
    out, err = run_tool(ctx, "call_me", {"text": "  Your  flight\nboards now. "})
    assert not err, out
    assert json.loads(out) == {"called": "+14165550100", "call_id": "CA42", "text": "Your flight boards now."}
    assert calls == [("+14165550100", "Your flight boards now.")]

    out, err = run_tool(ctx, "call_me", {"text": "   "})
    assert err and "empty" in out
    out, err = run_tool(ToolContext(store, config=_voice_config()), "call_me", {"text": "hi"})
    assert err and "not set up" in out
    out, err = run_tool(ToolContext(store, place_call=place_call), "call_me", {"text": "hi"})
    assert err and "not set up" in out  # no USER_PHONE without a config

    def broken(to_number: str, text: str) -> str:
        raise RuntimeError("Twilio refused the call")

    out, err = run_tool(ToolContext(store, config=_voice_config(), place_call=broken), "call_me", {"text": "hi"})
    assert err and "Twilio refused" in out
