from __future__ import annotations

import json

from assistant.store import Store
from assistant.tools import TOOLS, run_tool, tool_definitions


def test_definitions_are_well_formed() -> None:
    defs = tool_definitions()
    names = [d["name"] for d in defs]
    assert len(names) == len(set(names))
    for d in defs:
        assert d["description"]
        assert d["input_schema"]["type"] == "object"
    assert tool_definitions(web_search=True)[-1]["name"] == "web_search"
    assert len(defs) == len(TOOLS)


def test_task_roundtrip_through_tools(store: Store) -> None:
    out, err = run_tool(store, "add_task", {"title": "Call mom", "due": "2026-09-12"})
    assert not err
    task = json.loads(out)
    out, err = run_tool(store, "list_tasks", {})
    assert json.loads(out)["count"] == 1
    out, err = run_tool(store, "update_task", {"id": task["id"], "status": "done"})
    assert json.loads(out)["status"] == "done"
    out, err = run_tool(store, "list_tasks", {"status": "open"})
    assert json.loads(out)["count"] == 0


def test_errors_are_reported_not_raised(store: Store) -> None:
    out, err = run_tool(store, "delete_task", {"id": 999})
    assert err and "no task" in out
    out, err = run_tool(store, "add_task", {})
    assert err and "title" in out
    out, err = run_tool(store, "nope", {})
    assert err


def test_notes_events_memories(store: Store) -> None:
    out, _ = run_tool(store, "add_note", {"title": "Idea", "body": "Build a birdhouse", "tags": ["diy"]})
    note = json.loads(out)
    out, _ = run_tool(store, "search_notes", {"query": "birdhouse"})
    assert json.loads(out)["notes"][0]["id"] == note["id"]

    out, _ = run_tool(store, "add_event", {"title": "Lunch", "start": "2026-09-12T12:00"})
    out, _ = run_tool(store, "list_events", {"start": "2026-09-12", "end": "2026-09-13"})
    assert json.loads(out)["count"] == 1

    out, _ = run_tool(store, "remember", {"content": "Vegetarian", "category": "preference"})
    mem = json.loads(out)
    out, _ = run_tool(store, "list_memories", {})
    assert json.loads(out)["count"] == 1
    out, err = run_tool(store, "forget", {"id": mem["id"]})
    assert not err


def test_current_datetime(store: Store) -> None:
    out, err = run_tool(store, "get_current_datetime", {})
    data = json.loads(out)
    assert not err and data["weekday"] and "T" in data["datetime"]
