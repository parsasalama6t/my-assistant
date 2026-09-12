"""Tool definitions the assistant can call, plus a dispatcher that runs them.

Each tool is a plain JSON-schema definition (sent to the API) paired with a
handler that receives the Store and the tool input and returns a string.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING, Any, Callable

from assistant.google_client import GoogleClient, GoogleNotConnected
from assistant.schedule_rules import (
    REPEAT_RULES,
    format_local,
    next_occurrence,
    parse_local_datetime,
    parse_repeat,
    to_utc_iso,
)
from assistant.store import AUTO_SOURCE_PREFIXES, SCHEDULE_PRIORITIES, Store

if TYPE_CHECKING:
    from assistant.config import Config


@dataclass
class ToolContext:
    """Everything a tool handler may need.

    `channel`/`chat_id` identify the chat a request came from (None for the CLI
    or web UI); `config` supplies the default messaging target and allowlists;
    `place_call(to_number, text)` rings the user's phone and returns a call id
    (None when phone calls are not set up).
    """

    store: Store
    google: GoogleClient | None = None
    tz: tzinfo = timezone.utc
    channel: str | None = None
    chat_id: str | None = None
    config: "Config | None" = None
    place_call: Callable[[str, str], Any] | None = None


Handler = Callable[["ToolContext", dict[str, Any]], Any]


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
_ONE_MINUTE = timedelta(minutes=1)


# ----------------------------------------------------------------- handlers
def _tz_name(tz: tzinfo, at: datetime | None = None) -> str:
    return getattr(tz, "key", None) or (at or datetime.now(tz)).tzname() or str(tz)


def _now(ctx: ToolContext, args: dict[str, Any]) -> str:
    local = datetime.now(ctx.tz)
    return _dump(
        {
            "datetime": local.replace(microsecond=0).isoformat(),
            "weekday": local.strftime("%A"),
            "timezone": _tz_name(ctx.tz, local),
        }
    )


def _add_task(ctx: ToolContext, args: dict[str, Any]) -> str:
    return _dump(
        ctx.store.add_task(
            title=args["title"],
            due=args.get("due"),
            priority=args.get("priority") or "normal",
            notes=args.get("notes") or "",
        )
    )


def _list_tasks(ctx: ToolContext, args: dict[str, Any]) -> str:
    tasks = ctx.store.list_tasks(status=args.get("status") or "open", due_before=args.get("due_before"))
    return _dump({"count": len(tasks), "tasks": tasks})


def _update_task(ctx: ToolContext, args: dict[str, Any]) -> str:
    task = ctx.store.update_task(
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


def _delete_task(ctx: ToolContext, args: dict[str, Any]) -> str:
    if not ctx.store.delete_task(int(args["id"])):
        raise ValueError(f"no task with id {args['id']}")
    return _dump({"deleted": int(args["id"])})


def _add_note(ctx: ToolContext, args: dict[str, Any]) -> str:
    return _dump(ctx.store.add_note(args["title"], args.get("body") or "", args.get("tags") or []))


def _search_notes(ctx: ToolContext, args: dict[str, Any]) -> str:
    notes = ctx.store.search_notes(args.get("query") or "", limit=int(args.get("limit") or 20))
    return _dump({"count": len(notes), "notes": notes})


def _update_note(ctx: ToolContext, args: dict[str, Any]) -> str:
    note = ctx.store.update_note(
        int(args["id"]), title=args.get("title"), body=args.get("body"), tags=args.get("tags")
    )
    if note is None:
        raise ValueError(f"no note with id {args['id']}")
    return _dump(note)


def _delete_note(ctx: ToolContext, args: dict[str, Any]) -> str:
    if not ctx.store.delete_note(int(args["id"])):
        raise ValueError(f"no note with id {args['id']}")
    return _dump({"deleted": int(args["id"])})


def _add_event(ctx: ToolContext, args: dict[str, Any]) -> str:
    return _dump(
        ctx.store.add_event(
            title=args["title"],
            start=args["start"],
            end=args.get("end"),
            location=args.get("location") or "",
            notes=args.get("notes") or "",
        )
    )


def _list_events(ctx: ToolContext, args: dict[str, Any]) -> str:
    events = ctx.store.list_events(args.get("start"), args.get("end"), limit=int(args.get("limit") or 50))
    return _dump({"count": len(events), "events": events})


def _delete_event(ctx: ToolContext, args: dict[str, Any]) -> str:
    if not ctx.store.delete_event(int(args["id"])):
        raise ValueError(f"no event with id {args['id']}")
    return _dump({"deleted": int(args["id"])})


def _remember(ctx: ToolContext, args: dict[str, Any]) -> str:
    return _dump(ctx.store.add_memory(args["content"], args.get("category") or "general"))


def _list_memories(ctx: ToolContext, args: dict[str, Any]) -> str:
    memories = ctx.store.list_memories()
    return _dump({"count": len(memories), "memories": memories})


def _forget(ctx: ToolContext, args: dict[str, Any]) -> str:
    if not ctx.store.delete_memory(int(args["id"])):
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

# ------------------------------------------------------------ google tools
def _google(ctx: ToolContext) -> GoogleClient:
    if ctx.google is None:
        raise GoogleNotConnected("Google is not connected. Run: my-assistant google login")
    return ctx.google


def _gcal_list(ctx: ToolContext, args: dict[str, Any]) -> str:
    events = _google(ctx).list_events(args.get("start"), args.get("end"), int(args.get("limit") or 25))
    return _dump({"count": len(events), "events": events})


def _gcal_create(ctx: ToolContext, args: dict[str, Any]) -> str:
    return _dump(
        _google(ctx).create_event(
            title=args["title"],
            start=args["start"],
            end=args.get("end"),
            location=args.get("location") or "",
            description=args.get("description") or "",
            attendees=args.get("attendees") or [],
        )
    )


def _gcal_delete(ctx: ToolContext, args: dict[str, Any]) -> str:
    _google(ctx).delete_event(args["id"])
    return _dump({"deleted": args["id"]})


def _gmail_search(ctx: ToolContext, args: dict[str, Any]) -> str:
    messages = _google(ctx).search_messages(args.get("query") or "", int(args.get("limit") or 10))
    return _dump({"count": len(messages), "messages": messages})


def _gmail_read(ctx: ToolContext, args: dict[str, Any]) -> str:
    return _dump(_google(ctx).get_message(args["id"]))


def _gmail_draft(ctx: ToolContext, args: dict[str, Any]) -> str:
    return _dump(
        _google(ctx).create_draft(
            list(args["to"]), args.get("subject") or "", args["body"], args.get("reply_to_message_id")
        )
    )


def _gmail_send(ctx: ToolContext, args: dict[str, Any]) -> str:
    return _dump(
        _google(ctx).send_message(
            list(args["to"]), args.get("subject") or "", args["body"], args.get("reply_to_message_id")
        )
    )


def _gmail_mark_read(ctx: ToolContext, args: dict[str, Any]) -> str:
    _google(ctx).mark_read(args["id"])
    return _dump({"marked_read": args["id"]})


GOOGLE_TOOLS: list[Tool] = [
    Tool(
        "gcal_list_events",
        "List events from the user's Google Calendar, soonest first. Defaults to the next "
        "14 days. Use this (not list_events) for the user's real calendar.",
        _obj(
            {
                "start": {"type": "string", "description": f"Range start, {DATETIME_HINT}"},
                "end": {"type": "string", "description": f"Range end, {DATETIME_HINT}"},
                "limit": {"type": "integer"},
            }
        ),
        _gcal_list,
    ),
    Tool(
        "gcal_create_event",
        "Create an event on the user's Google Calendar. Date-only start makes an all-day "
        "event; a time without an end defaults to one hour.",
        _obj(
            {
                "title": {"type": "string"},
                "start": {"type": "string", "description": f"Start, {DATETIME_HINT}"},
                "end": {"type": "string", "description": f"Optional end, {DATETIME_HINT}"},
                "location": {"type": "string"},
                "description": {"type": "string"},
                "attendees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Email addresses to invite.",
                },
            },
            ["title", "start"],
        ),
        _gcal_create,
    ),
    Tool(
        "gcal_delete_event",
        "Delete an event from the user's Google Calendar by its id (from gcal_list_events). "
        "Confirm with the user first unless they clearly asked for the deletion.",
        _obj({"id": {"type": "string"}}, ["id"]),
        _gcal_delete,
    ),
    Tool(
        "gmail_search",
        "Search the user's Gmail. Uses Gmail search syntax, e.g. 'is:unread', "
        "'from:alice@example.com', 'subject:invoice newer_than:7d', 'in:inbox'. Empty query "
        "returns the most recent messages. Returns headers and a snippet, not full bodies.",
        _obj({"query": {"type": "string"}, "limit": {"type": "integer"}}),
        _gmail_search,
    ),
    Tool(
        "gmail_read",
        "Read the full text of one email by message id (from gmail_search).",
        _obj({"id": {"type": "string"}}, ["id"]),
        _gmail_read,
    ),
    Tool(
        "gmail_draft",
        "Create a Gmail draft (not sent). Prefer this over gmail_send unless the user "
        "explicitly asked you to send. Pass reply_to_message_id to draft a reply in-thread.",
        _obj(
            {
                "to": {"type": "array", "items": {"type": "string"}},
                "subject": {"type": "string"},
                "body": {"type": "string", "description": "Plain-text body."},
                "reply_to_message_id": {"type": "string"},
            },
            ["to", "body"],
        ),
        _gmail_draft,
    ),
    Tool(
        "gmail_send",
        "Send an email from the user's Gmail. Only call this after the user has explicitly "
        "asked to send and has seen the recipient, subject, and body. If unsure, use "
        "gmail_draft instead. Pass reply_to_message_id to reply in-thread.",
        _obj(
            {
                "to": {"type": "array", "items": {"type": "string"}},
                "subject": {"type": "string"},
                "body": {"type": "string", "description": "Plain-text body."},
                "reply_to_message_id": {"type": "string"},
            },
            ["to", "body"],
        ),
        _gmail_send,
    ),
    Tool(
        "gmail_mark_read",
        "Mark an email as read by message id.",
        _obj({"id": {"type": "string"}}, ["id"]),
        _gmail_mark_read,
    ),
]

GOOGLE_TOOL_NAMES = {t.name for t in GOOGLE_TOOLS}


# ---------------------------------------------------------- schedule tools
def _target(ctx: ToolContext) -> tuple[str, str]:
    """Resolve where a scheduled text goes: the current chat, else the configured default."""
    if ctx.channel and ctx.chat_id:
        target = (ctx.channel, str(ctx.chat_id))
    elif ctx.config is not None and ctx.config.default_target() is not None:
        target = ctx.config.default_target()  # type: ignore[assignment]
    else:
        raise ValueError(
            "No messaging channel is configured, so scheduled texts cannot be delivered. "
            "Set TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID or the TWILIO_* variables and USER_PHONE."
        )
    if ctx.config is not None:
        allowed = ctx.config.allowed_chat_ids(target[0])
        if allowed and target[1] not in allowed:
            raise ValueError(f"{target[1]} is not an allowed recipient on {target[0]}")
    return target


def _schedule_view(row: dict[str, Any], tz: tzinfo) -> dict[str, Any]:
    out = {
        "id": row["id"],
        "kind": row["kind"],
        "text": row["text"],
        "next_run": format_local(row["next_run_utc"], tz),
        "repeat": row["repeat"],
        "channel": row["channel"],
        "priority": row.get("priority") or "normal",
    }
    if row.get("source"):
        out["source"] = row["source"]
    return out


def _schedule_message(ctx: ToolContext, args: dict[str, Any]) -> str:
    text = (args.get("text") or "").strip()
    if not text:
        raise ValueError("text cannot be empty")
    priority = (args.get("priority") or "normal").strip().lower()
    if priority not in SCHEDULE_PRIORITIES:
        raise ValueError(f"priority must be one of {', '.join(SCHEDULE_PRIORITIES)}; got {priority!r}")
    channel, chat_id = _target(ctx)
    first = parse_local_datetime(args.get("when") or "", ctx.tz)
    now = datetime.now(timezone.utc)
    rule = parse_repeat(args.get("repeat"), weekday=first.astimezone(ctx.tz).weekday())
    local_time: str | None = None
    if rule:
        local_time = first.astimezone(ctx.tz).strftime("%H:%M")
        if first <= now:
            first = next_occurrence(rule, local_time, now, ctx.tz)
        else:
            first = next_occurrence(rule, local_time, first - _ONE_MINUTE, ctx.tz)
    elif first <= now:
        raise ValueError(
            f"{args.get('when')} is in the past (now is {now.astimezone(ctx.tz).strftime('%Y-%m-%d %H:%M')})"
        )
    row = ctx.store.add_schedule(
        kind="custom",
        channel=channel,
        chat_id=chat_id,
        next_run_utc=to_utc_iso(first),
        tz=_tz_name(ctx.tz),
        text=text,
        repeat=rule,
        local_time=local_time,
        priority=priority,
    )
    view = _schedule_view(row, ctx.tz)
    if priority != "normal" and not (ctx.config is not None and ctx.config.has_voice()):
        view["note"] = "Phone calls are not set up, so this will be a text only."
    return _dump(view)


def _list_scheduled(ctx: ToolContext, args: dict[str, Any]) -> str:
    rows = ctx.store.list_schedules(enabled_only=True, include_system=False)
    views = [_schedule_view(r, ctx.tz) for r in rows]
    return _dump({"count": len(views), "timezone": _tz_name(ctx.tz), "scheduled": views})


def _cancel_scheduled(ctx: ToolContext, args: dict[str, Any]) -> str:
    schedule_id = int(args["id"])
    row = ctx.store.get_schedule(schedule_id)
    if row is None or not row["enabled"]:
        raise ValueError(f"no active scheduled message with id {schedule_id}")
    source = row.get("source") or ""
    if source.startswith(AUTO_SOURCE_PREFIXES):
        raise ValueError(
            "That reminder is generated automatically from a task or calendar event. "
            "Mark the task done, change its due time, or delete the event instead."
        )
    ctx.store.cancel_schedule(schedule_id)
    return _dump({"cancelled": schedule_id, "text": row["text"], "kind": row["kind"]})


PRIORITY_HINT = (
    "normal = a text. important = a text, then a phone call if the user has not replied "
    "within about 10 minutes. critical = a phone call right away plus the text. Default "
    "normal; use important or critical only when the user signals it matters (deadlines, "
    "flights, medication, 'make sure', 'call me')."
)

SCHEDULE_TOOLS: list[Tool] = [
    Tool(
        "schedule_message",
        "Schedule a text message to the user's phone at a local date/time, once or on a "
        "repeat. Call get_current_datetime first so 'when' is right. Messages are phone "
        "texts: keep the text short and self-contained (it is sent verbatim, with no "
        "other context). Tasks with due times and calendar events are texted "
        "automatically, so do not schedule duplicates for those. Priority controls "
        "whether the phone also rings: " + PRIORITY_HINT + "\n" + REPEAT_RULES,
        _obj(
            {
                "text": {"type": "string", "description": "The message to send, verbatim."},
                "when": {
                    "type": "string",
                    "description": f"Local date/time of the (first) send, {DATETIME_HINT}",
                },
                "repeat": {
                    "type": "string",
                    "description": "Optional: daily | weekdays | weekends | weekly | days:mon,wed",
                },
                "priority": {
                    "type": "string",
                    "enum": list(SCHEDULE_PRIORITIES),
                    "description": PRIORITY_HINT,
                },
            },
            ["text", "when"],
        ),
        _schedule_message,
    ),
    Tool(
        "list_scheduled",
        "List scheduled messages (the user's own plus the daily briefing/review) with "
        "their ids and next send time in the user's timezone.",
        _obj({}),
        _list_scheduled,
    ),
    Tool(
        "cancel_scheduled",
        "Cancel a scheduled message by id (from list_scheduled). Automatic task/event "
        "reminders cannot be cancelled here; change the task or event instead.",
        _obj({"id": {"type": "integer"}}, ["id"]),
        _cancel_scheduled,
    ),
]


# ------------------------------------------------------------- voice tools
def _call_me(ctx: ToolContext, args: dict[str, Any]) -> str:
    text = " ".join((args.get("text") or "").split())
    if not text:
        raise ValueError("text cannot be empty")
    to_number = ctx.config.user_phone if ctx.config is not None else ""
    if ctx.place_call is None or not to_number:
        raise ValueError(
            "Phone calls are not set up. Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, a voice number "
            "(TWILIO_VOICE_FROM or TWILIO_FROM) and USER_PHONE, then restart."
        )
    call_id = ctx.place_call(to_number, text)
    return _dump({"called": to_number, "call_id": call_id, "text": text})


VOICE_TOOLS: list[Tool] = [
    Tool(
        "call_me",
        "Place a phone call to the user right now and read `text` aloud (twice), then hang "
        "up. Reserve this for things that genuinely matter or when the user asks to be "
        "called; a text is the default. Keep the text to one or two spoken sentences.",
        _obj({"text": {"type": "string", "description": "What to say on the call, verbatim."}}, ["text"]),
        _call_me,
    ),
]

VOICE_TOOL_NAMES = {t.name for t in VOICE_TOOLS}

def _delivery_log(ctx: ToolContext, args: dict[str, Any]) -> str:
    from assistant.schedule_rules import format_local

    rows = ctx.store.list_deliveries(int(args.get("limit") or 20))
    out = []
    for r in rows:
        sched = ctx.store.get_schedule(r["schedule_id"]) if r.get("schedule_id") else None
        out.append(
            {
                "when_local": format_local(r["fire_at_utc"], ctx.tz) if r.get("fire_at_utc") else None,
                "status": r["status"],
                "kind": r.get("kind"),
                "text": (sched or {}).get("text", ""),
                "reason": r.get("error"),
            }
        )
    return _dump({"count": len(out), "deliveries": out, "note": "status 'skipped' with 'missed beyond grace' means the assistant was offline when it was due"})


SCHEDULE_TOOLS.append(
    Tool(
        "delivery_log",
        "Show which scheduled texts and calls were actually sent, skipped, or failed, with "
        "the reason and local time. Use this before answering questions like 'why didn't I "
        "get my 3pm reminder'. 'missed beyond grace' means the assistant was offline (for "
        "example the computer was asleep) when it was due.",
        _obj({"limit": {"type": "integer"}}),
        _delivery_log,
    )
)

TOOLS_BY_NAME: dict[str, Tool] = {
    t.name: t for t in [*TOOLS, *GOOGLE_TOOLS, *SCHEDULE_TOOLS, *VOICE_TOOLS]
}

WEB_SEARCH_TOOL: dict[str, Any] = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": 5,
}


def tool_definitions(
    web_search: bool = False, google: bool = False, scheduling: bool = False, voice: bool = False
) -> list[dict[str, Any]]:
    """Tool list to send with each request. Order is stable so prompt caching works."""
    defs = [t.definition() for t in TOOLS]
    if google:
        defs.extend(t.definition() for t in GOOGLE_TOOLS)
    if scheduling:
        defs.extend(t.definition() for t in SCHEDULE_TOOLS)
    if voice:
        defs.extend(t.definition() for t in VOICE_TOOLS)
    if web_search:
        defs.append(WEB_SEARCH_TOOL)
    return defs


def run_tool(ctx: ToolContext, name: str, args: dict[str, Any]) -> tuple[str, bool]:
    """Execute a tool. Returns (result_text, is_error)."""
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        return f"Error: unknown tool '{name}'", True
    try:
        return str(tool.handler(ctx, args or {})), False
    except (KeyError, ValueError, TypeError, GoogleNotConnected) as exc:
        return f"Error: {exc}", True
    except Exception as exc:  # noqa: BLE001 - Google/Twilio API failures surface to the model
        if name in GOOGLE_TOOL_NAMES:
            return f"Error talking to Google: {exc}", True
        if name in VOICE_TOOL_NAMES:
            return f"Error placing the call: {exc}", True
        raise
