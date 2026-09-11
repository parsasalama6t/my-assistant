"""Command-line interface: interactive chat plus quick subcommands."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from assistant import __version__
from assistant.agent import Assistant, AssistantError
from assistant.config import Config
from assistant.store import Store

BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"

BRIEFING_PROMPT = (
    "Give me my briefing for today. Check the current date, then list today's and "
    "tomorrow's events, overdue and due-soon tasks, and anything else from what you "
    "remember about me that's relevant today. Be concise; if a section is empty, skip it."
)

HELP_TEXT = f"""{BOLD}Commands{RESET}
  /new        start a fresh conversation
  /tasks      show open tasks
  /notes      show recent notes
  /events     show upcoming events
  /memories   show what the assistant remembers
  /help       show this help
  /quit       exit (or Ctrl-D)
Anything else is sent to your assistant."""


def _color(enabled: bool):
    return (lambda s, c: f"{c}{s}{RESET}") if enabled else (lambda s, c: s)


def _fmt_task(t: dict[str, Any]) -> str:
    mark = "x" if t["status"] == "done" else " "
    due = f"  due {t['due']}" if t.get("due") else ""
    pri = "  !" if t.get("priority") == "high" else ""
    return f"[{mark}] #{t['id']} {t['title']}{due}{pri}"


def _fmt_note(n: dict[str, Any]) -> str:
    tags = f"  [{n['tags']}]" if n.get("tags") else ""
    body = n["body"].strip().splitlines()[0] if n.get("body") else ""
    body = f"  - {body[:80]}" if body else ""
    return f"#{n['id']} {n['title']}{tags}{body}"


def _fmt_event(e: dict[str, Any]) -> str:
    when = e["start"] + (f" to {e['end']}" if e.get("end") else "")
    loc = f"  @ {e['location']}" if e.get("location") else ""
    return f"#{e['id']} {when}  {e['title']}{loc}"


def _fmt_memory(m: dict[str, Any]) -> str:
    return f"#{m['id']} [{m['category']}] {m['content']}"


def _print_list(title: str, rows: list[dict[str, Any]], fmt) -> None:
    print(f"{BOLD}{title}{RESET}")
    if not rows:
        print(f"  {DIM}(none){RESET}")
        return
    for r in rows:
        print("  " + fmt(r))


# ----------------------------------------------------------------- commands
def cmd_chat(args: argparse.Namespace, store: Store, config: Config) -> int:
    color = _color(sys.stdout.isatty())
    assistant = Assistant(store, config)
    if args.new or not assistant.history():
        assistant.new_session()
    print(color(f"my-assistant {__version__} · {config.model} · effort {config.effort}", DIM))
    print(color("Type /help for commands, /quit to exit.", DIM))

    def on_tool(name: str, tool_input: dict[str, Any], output: str, is_error: bool) -> None:
        marker = color("error", RED) if is_error else color("ok", GREEN)
        print(color(f"  ⚙ {name} {marker}", DIM))

    while True:
        try:
            user_text = input(color("\nyou> ", CYAN)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not user_text:
            continue
        if user_text.startswith("/"):
            command = user_text.split()[0].lower()
            if command in {"/quit", "/exit", "/q"}:
                return 0
            if command == "/help":
                print(HELP_TEXT)
            elif command == "/new":
                assistant.new_session()
                print(color("Started a new conversation.", DIM))
            elif command == "/tasks":
                _print_list("Open tasks", store.list_tasks("open"), _fmt_task)
            elif command == "/notes":
                _print_list("Recent notes", store.search_notes("", limit=10), _fmt_note)
            elif command == "/events":
                _print_list("Events", store.list_events(limit=20), _fmt_event)
            elif command == "/memories":
                _print_list("Memories", store.list_memories(), _fmt_memory)
            else:
                print(color(f"Unknown command {command}. Try /help.", RED))
            continue

        print(color("\nassistant> ", GREEN), end="", flush=True)
        try:
            result = assistant.chat(
                user_text,
                on_text=lambda chunk: print(chunk, end="", flush=True),
                on_tool=on_tool,
            )
        except AssistantError as exc:
            print(color(f"\n{exc}", RED))
            continue
        except KeyboardInterrupt:
            print(color("\n(interrupted)", DIM))
            continue
        if result.refusal and result.text == result.refusal:
            print(result.text, end="")
        print()


def cmd_ask(args: argparse.Namespace, store: Store, config: Config) -> int:
    assistant = Assistant(store, config)
    if args.new:
        assistant.new_session()
    try:
        assistant.chat(" ".join(args.text), on_text=lambda c: print(c, end="", flush=True))
    except AssistantError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1
    print()
    return 0


def cmd_briefing(args: argparse.Namespace, store: Store, config: Config) -> int:
    assistant = Assistant(store, config)
    assistant.new_session("briefing")
    try:
        assistant.chat(BRIEFING_PROMPT, on_text=lambda c: print(c, end="", flush=True))
    except AssistantError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1
    print()
    return 0


def cmd_tasks(args: argparse.Namespace, store: Store, config: Config) -> int:
    status = "all" if args.all else "open"
    _print_list("Tasks" if args.all else "Open tasks", store.list_tasks(status), _fmt_task)
    return 0


def cmd_notes(args: argparse.Namespace, store: Store, config: Config) -> int:
    query = " ".join(args.query)
    _print_list("Notes", store.search_notes(query), _fmt_note)
    return 0


def cmd_events(args: argparse.Namespace, store: Store, config: Config) -> int:
    _print_list("Events", store.list_events(), _fmt_event)
    return 0


def cmd_memories(args: argparse.Namespace, store: Store, config: Config) -> int:
    _print_list("Memories", store.list_memories(), _fmt_memory)
    return 0


def cmd_sessions(args: argparse.Namespace, store: Store, config: Config) -> int:
    rows = store.list_sessions()
    _print_list(
        "Conversations",
        rows,
        lambda s: f"{s['id']}  {s['updated_at']}  {s['message_count']} messages  {s['title']}",
    )
    return 0


def cmd_serve(args: argparse.Namespace, store: Store, config: Config) -> int:
    try:
        import uvicorn

        from assistant.server import create_app
    except ImportError:
        print("The web UI needs extra packages: pip install 'my-assistant[web]'", file=sys.stderr)
        return 1
    app = create_app(store, config)
    print(f"Web UI at http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


# ------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="my-assistant", description="Your personal assistant.")
    parser.add_argument("--version", action="version", version=f"my-assistant {__version__}")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("chat", help="interactive chat (default)")
    p.add_argument("--new", action="store_true", help="start a fresh conversation")
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("ask", help="send one message and print the reply")
    p.add_argument("text", nargs="+")
    p.add_argument("--new", action="store_true", help="start a fresh conversation")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("briefing", help="print a briefing for today")
    p.set_defaults(func=cmd_briefing)

    p = sub.add_parser("tasks", help="list tasks")
    p.add_argument("--all", action="store_true", help="include completed tasks")
    p.set_defaults(func=cmd_tasks)

    p = sub.add_parser("notes", help="list or search notes")
    p.add_argument("query", nargs="*")
    p.set_defaults(func=cmd_notes)

    p = sub.add_parser("events", help="list calendar events")
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("memories", help="list what the assistant remembers")
    p.set_defaults(func=cmd_memories)

    p = sub.add_parser("sessions", help="list past conversations")
    p.set_defaults(func=cmd_sessions)

    p = sub.add_parser("serve", help="run the web UI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args(["chat", *(argv or [])])
    try:
        config = Config.from_env()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
    store = Store(config.db_path)
    try:
        sys.exit(args.func(args, store, config) or 0)
    finally:
        store.close()
