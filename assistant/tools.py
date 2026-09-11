"""Tool definitions the assistant can call, plus a dispatcher that runs them.

Each tool is a plain JSON-schema definition (sent to the API) paired with a
handler that receives the Store and the tool input and returns a string.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from assistant.store import Store

Handler = Callable[[Store, dict[str, Any]], Any]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=None, default=str)


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


DATETIME_HINT = "ISO 8601 (e.g. 2026-09-14 or 2026-09-14T15:30)."


# ----------------------------------------------------------------- handlers
def _now(store: Store, args: dict[str, Any]) -> str:
    local = datetime.now().astimezone()
    return _dump(
        {
            "datetime": local.replace(microsecond=0).isoformat(),
            "weekday": local.strftime("%A"),
            "timezone": local.tzname(),
        }
    )


def _add_task(store: Store, args: dict[str, Any]) -> str:
    return _dump(
        store.add_task(
            title=args["title"],
            due=args.get("due"),
            priority=args.get("priority") or "normal",
            notes=args.get("notes") or "",
        )
    )


def _list_tasks(store: Store, args: dict[str, Any]) -> str:
    tasks = store.list_tasks(status=args.get("status") or "open", due_before=args.get("due_before"))
    return _dump({"count": len(tasks), "tasks": tasks})


def _update_task(store: Store, args: dict[str, Any]) -> str:
    task = store.update_task(
        int(args["id"]),
        title=args.get("title"),
        notes=args.get("notes"),
        due=args.get("due"),
        priority=args.get("priority"),
        status=args.get("status"),
    )
    if task is None:
        raise ValueError(f"no task with id {args['id']}")
    return _dump(task)


def _delete_task(store: Store, args: dict[str, Any]) -> str:
    if not store.delete_task(int(args["id"])):
        raise ValueError(f"no task with id {args['id']}")
    return _dump({"deleted": int(args["id"])})


def _add_note(store: Store, args: dict[str, Any]) -> str:
    return _dump(store.add_note(args["title"], args.get("body") or "", args.get("tags") or []))


def _search_notes(store: Store, args: dict[str, Any]) -> str:
    notes = store.search_notes(args.get("query") or "", limit=int(args.get("limit") or 20))
    return _dump({"count": len(notes), "notes": notes})


def _update_note(store: Store, args: dict[str, Any]) -> str:
    note = store.update_note(
        int(args["id"]), title=args.get("title"), body=args.get("body"), tags=args.get("tags")
    )
    if note is None:
        raise ValueError(f"no note with id {args['id']}")
    return _dump(note)


def _delete_note(store: Store, args: dict[str, Any]) -> str:
    if not store.delete_note(int(args["id"])):
        raise ValueError(f"no note with id {args['id']}")
    return _dump({"deleted": int(args["id"])})


def _add_event(store: Store, args: dict[str, Any]) -> str:
    return _dump(
        store.add_event(
            title=args["title"],
            start=args["start"],
            end=args.get("end"),
            location=args.get("location") or "",
            notes=args.get("notes") or "",
        )
    )


def _list_events(store: Store, args: dict[str, Any]) -> str:
    events = store.list_events(args.get("start"), args.get("end"), limit=int(args.get("limit") or 50))
    return _dump({"count": len(events), "events": events})


def _delete_event(store: Store, args: dict[str, Any]) -> str:
    if not store.delete_event(int(args["id"])):
        raise ValueError(f"no event with id {args['id']}")
    return _dump({"deleted": int(args["id"])})


def _remember(store: Store, args: dict[str, Any]) -> str:
    return _dump(store.add_memory(args["content"], args.get("category") or "general"))


def _list_memories(store: Store, args: dict[str, Any]) -> str:
    memories = store.list_memories()
    return _dump({"count": len(memories), "memories": memories})


def _forget(store: Store, args: dict[str, Any]) -> str:
    if not store.delete_memory(int(args["id"])):
        raise ValueError(f"no memory with id {args['id']}")
    return _dump({"deleted": int(args["id"])})


# ---------------------------------------------------------------- registry
TOOLS: list[Tool] = [
    Tool(
        "get_current_datetime",
        "Get the current local date, time, weekday, and timezone. Call this before "
        "interpreting relative dates like 'tomorrow' or 'next Friday'.",
        _obj({}),
        _now,
    ),
    Tool(
        "add_task",
        "Add a to-do item for the user.",
        _obj(
            {
                "title": {"type": "string", "description": "Short description of the task."},
                "due": {"type": "string", "description": f"Optional due date/time, {DATETIME_HINT}"},
                "priority": {"type": "string", "enum": ["low", "normal", "high"]},
                "notes": {"type": "string", "description": "Optional extra details."},
            },
            ["title"],
        ),
        _add_task,
    ),
    Tool(
        "list_tasks",
        "List the user's tasks. Defaults to open tasks, soonest due first.",
        _obj(
            {
                "status": {"type": "string", "enum": ["open", "done", "all"]},
                "due_before": {
                    "type": "string",
                    "description": f"Only tasks due on or before this date/time, {DATETIME_HINT}",
                },
            }
        ),
        _list_tasks,
    ),
    Tool(
        "update_task",
        "Update a task: rename it, change its due date, priority, notes, or mark it "
        "done/open. Only provided fields change.",
        _obj(
            {
                "id": {"type": "integer"},
                "title": {"type": "string"},
                "due": {"type": "string", "description": DATETIME_HINT},
                "priority": {"type": "string", "enum": ["low", "normal", "high"]},
                "status": {"type": "string", "enum": ["open", "done"]},
                "notes": {"type": "string"},
            },
            ["id"],
        ),
        _update_task,
    ),
    Tool(
        "delete_task",
        "Permanently delete a task by id. Prefer marking tasks done unless the user asks to delete.",
        _obj({"id": {"type": "integer"}}, ["id"]),
        _delete_task,
    ),
    Tool(
        "add_note",
        "Save a note for the user (ideas, meeting notes, reference info, lists).",
        _obj(
            {
                "title": {"type": "string"},
                "body": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            ["title"],
        ),
        _add_note,
    ),
    Tool(
        "search_notes",
        "Search notes by keyword across title, body, and tags. Omit the query to list recent notes.",
        _obj({"query": {"type": "string"}, "limit": {"type": "integer"}}),
        _search_notes,
    ),
    Tool(
        "update_note",
        "Edit an existing note's title, body, or tags. Only provided fields change.",
        _obj(
            {
                "id": {"type": "integer"},
                "title": {"type": "string"},
                "body": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            ["id"],
        ),
        _update_note,
    ),
    Tool(
        "delete_note",
        "Permanently delete a note by id.",
        _obj({"id": {"type": "integer"}}, ["id"]),
        _delete_note,
    ),
    Tool(
        "add_event",
        "Add a calendar event, appointment, or reminder at a specific time.",
        _obj(
            {
                "title": {"type": "string"},
                "start": {"type": "string", "description": f"Start, {DATETIME_HINT}"},
                "end": {"type": "string", "description": f"Optional end, {DATETIME_HINT}"},
                "location": {"type": "string"},
                "notes": {"type": "string"},
            },
            ["title", "start"],
        ),
        _add_event,
    ),
    Tool(
        "list_events",
        "List calendar events in a date range, soonest first. Omit both bounds to list everything.",
        _obj(
            {
                "start": {"type": "string", "description": f"Range start, {DATETIME_HINT}"},
                "end": {"type": "string", "description": f"Range end, {DATETIME_HINT}"},
                "limit": {"type": "integer"},
            }
        ),
        _list_events,
    ),
    Tool(
        "delete_event",
        "Delete a calendar event by id.",
        _obj({"id": {"type": "integer"}}, ["id"]),
        _delete_event,
    ),
    Tool(
        "remember",
        "Save a durable fact about the user for future conversations (preferences, people, "
        "routines, goals, how they like things done). Use this whenever the user shares "
        "something worth remembering long-term. Do not store secrets or passwords.",
        _obj(
            {
                "content": {"type": "string", "description": "One clear, self-contained fact."},
                "category": {
                    "type": "string",
                    "description": "e.g. preference, person, work, health, goal, general",
                },
            },
            ["content"],
        ),
        _remember,
    ),
    Tool(
        "list_memories",
        "List everything the assistant remembers about the user.",
        _obj({}),
        _list_memories,
    ),
    Tool(
        "forget",
        "Delete a remembered fact by id, e.g. when it is outdated or the user asks you to forget it.",
        _obj({"id": {"type": "integer"}}, ["id"]),
        _forget,
    ),
]

TOOLS_BY_NAME: dict[str, Tool] = {t.name: t for t in TOOLS}

WEB_SEARCH_TOOL: dict[str, Any] = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": 5,
}


def tool_definitions(web_search: bool = False) -> list[dict[str, Any]]:
    """Tool list to send with each request. Order is stable so prompt caching works."""
    defs = [t.definition() for t in TOOLS]
    if web_search:
        defs.append(WEB_SEARCH_TOOL)
    return defs


def run_tool(store: Store, name: str, args: dict[str, Any]) -> tuple[str, bool]:
    """Execute a tool. Returns (result_text, is_error)."""
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        return f"Error: unknown tool '{name}'", True
    try:
        return str(tool.handler(store, args or {})), False
    except (KeyError, ValueError, TypeError) as exc:
        return f"Error: {exc}", True
