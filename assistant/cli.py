"""Command-line interface: interactive chat plus quick subcommands."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from assistant import __version__
from assistant.agent import Assistant, AssistantError
from assistant.config import DEFAULT_TIMEZONE, Config, validate_hhmm
from assistant.google_client import GoogleClient, GoogleNotConnected
from assistant.prompts import BRIEFING_PROMPT
from assistant.schedule_rules import format_local
from assistant.store import Store

BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"

HELP_TEXT = f"""{BOLD}Commands{RESET}
  /new        start a fresh conversation
  /tasks      show open tasks
  /notes      show recent notes
  /events     show upcoming events
  /memories   show what the assistant remembers
  /help       show this help
  /quit       exit (or Ctrl-D)
Anything else is sent to your assistant."""


def load_google(config: Config, quiet: bool = False) -> GoogleClient | None:
    """Return a connected Google client, or None when not set up / disabled."""
    if not config.google or not GoogleClient.is_connected(config.data_dir):
        return None
    try:
        return GoogleClient.load(config.data_dir)
    except (GoogleNotConnected, RuntimeError, OSError, ValueError) as exc:
        if not quiet:
            print(f"Google integration unavailable: {exc}", file=sys.stderr)
        return None


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
    google = load_google(config)
    assistant = Assistant(store, config, google=google)
    if args.new or not assistant.history():
        assistant.new_session()
    status = f"google {google.email or 'connected'}" if google else "google off"
    print(color(f"my-assistant {__version__} · {config.model} · effort {config.effort} · {status}", DIM))
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
    assistant = Assistant(store, config, google=load_google(config))
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
    google = load_google(config)
    if getattr(args, "send", False):
        from assistant.channels import ChannelError
        from assistant.daemon import Daemon

        target = config.default_target()
        if target is None:
            print(_NO_CHANNELS_MESSAGE, file=sys.stderr)
            return 2
        daemon = Daemon(config, store_factory=lambda: store, google=google)
        channel, chat_id = target
        try:
            text = daemon.render_briefing("briefing", channel, chat_id)
            message_id = daemon.send(channel, chat_id, text)
        except AssistantError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except ChannelError as exc:
            print(f"Could not send the briefing over {channel}: {exc}", file=sys.stderr)
            return 1
        print(text)
        print(f"\nSent to {channel} {chat_id} (message {message_id}).")
        return 0
    assistant = Assistant(store, config, google=google)
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
    app = create_app(store, config, google=load_google(config))
    print(f"Web UI at http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_google(args: argparse.Namespace, store: Store, config: Config) -> int:
    creds = config.google_credentials_path
    token = GoogleClient.token_path(config.data_dir)
    if args.action == "login":
        try:
            client = GoogleClient.login(config.data_dir, creds)
        except (FileNotFoundError, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            print(
                "\nSetup: in Google Cloud Console enable the Google Calendar API and Gmail API, "
                "create an OAuth client of type 'Desktop app', download its JSON, and save it as\n"
                f"  {creds}",
                file=sys.stderr,
            )
            return 1
        print(f"Connected Google account {client.email or ''}".rstrip() + ".")
        return 0
    if args.action == "status":
        if not token.is_file():
            print("Google: not connected. Run: my-assistant google login")
            return 0
        client = load_google(config)
        if client is None:
            return 1
        print(f"Google: connected as {client.email or '(unknown account)'}")
        print(f"Token: {token}")
        return 0
    if args.action == "logout":
        if token.is_file():
            token.unlink()
            print("Disconnected Google (token removed).")
        else:
            print("Google was not connected.")
        return 0
    return 1


# ---------------------------------------------------- daemon and channels
_NO_CHANNELS_MESSAGE = (
    "No messaging channel is configured, so there is nothing to run.\n"
    "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (or the TWILIO_* variables and USER_PHONE)\n"
    "in .env; see .env.example, or run: my-assistant setup"
)


def _configure_logging() -> None:
    level_name = os.environ.get("ASSISTANT_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx2").setLevel(max(level, logging.WARNING))


def cmd_daemon(args: argparse.Namespace, store: Store, config: Config) -> int:
    from assistant.channels import ChannelError
    from assistant.daemon import Daemon

    _configure_logging()
    daemon = Daemon(config, google=load_google(config))
    if not daemon.channels:
        print(_NO_CHANNELS_MESSAGE, file=sys.stderr)
        return 2
    if args.once:
        try:
            report = daemon.run_once()
        finally:
            daemon.close()
        print(f"sent {len(report.sent)}, skipped {len(report.skipped)}, failed {len(report.failed)}")
        for item in report.failed:
            print(f"  failed #{item['id']} ({item['kind']}): {item.get('error')}")
        return 0
    try:
        return daemon.run()
    except ChannelError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def _channel_summary(config: Config) -> list[str]:
    lines = []
    if config.telegram_bot_token:
        ids = ", ".join(config.telegram_chat_ids) or "(no TELEGRAM_CHAT_ID yet: run `my-assistant channels whoami`)"
        lines.append(f"telegram: configured, chat ids {ids}")
    else:
        lines.append("telegram: not configured (TELEGRAM_BOT_TOKEN)")
    if config.twilio_account_sid and config.twilio_auth_token:
        senders = []
        if config.twilio_from:
            senders.append(f"SMS from {config.twilio_from}")
        if config.twilio_whatsapp_from:
            senders.append(f"WhatsApp from {config.twilio_whatsapp_from}")
        webhook = config.twilio_webhook_url or "(no TWILIO_WEBHOOK_URL: inbound messages off)"
        phone = config.user_phone or "(no USER_PHONE)"
        lines.append(f"twilio: configured, {'; '.join(senders) or 'no sender'}; to {phone}; webhook {webhook}")
    else:
        lines.append("twilio: not configured (TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN)")
    if config.has_voice():
        lines.append(f"voice: on (from {config.voice_from}, calls {config.user_phone})")
    else:
        missing = []
        if not (config.twilio_account_sid and config.twilio_auth_token):
            missing.append("TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN")
        if not config.voice_from:
            missing.append("TWILIO_VOICE_FROM or TWILIO_FROM")
        if not config.user_phone:
            missing.append("USER_PHONE")
        lines.append(f"voice: off (needs {', '.join(missing)})")
    return lines


def cmd_channels(args: argparse.Namespace, store: Store, config: Config) -> int:
    from assistant.channels import ChannelError, build_channels

    if args.action == "status":
        for line in _channel_summary(config):
            print(line)
        target = config.default_target()
        print(f"default target: {target[0]} {target[1]}" if target else "default target: none")
        print(f"timezone: {config.timezone}")
        print(f"morning briefing: {config.morning_briefing or 'off'}")
        print(f"evening review: {config.evening_review or 'off'}")
        return 0

    if args.action == "test" and getattr(args, "call", False):
        from assistant.voice import VoiceError, build_voice

        if not config.has_voice():
            print(
                "Phone calls are not configured. Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, a voice-capable\n"
                "number (TWILIO_VOICE_FROM or TWILIO_FROM) and USER_PHONE in .env, or run: my-assistant setup",
                file=sys.stderr,
            )
            return 2
        voice = build_voice(config)
        text = args.text or "Hello from my-assistant. Phone calls work."
        try:
            call_id = voice.call(config.user_phone, text)
        except VoiceError as exc:
            print(f"Call failed: {exc}", file=sys.stderr)
            return 1
        finally:
            voice.close()
        print(f"Calling {config.user_phone} from {config.voice_from} (call {call_id}).")
        return 0

    if args.action == "test":
        target = config.default_target()
        if target is None:
            print(_NO_CHANNELS_MESSAGE, file=sys.stderr)
            return 2
        channel, chat_id = target
        channels = build_channels(config, store)
        text = args.text or "Hello from my-assistant. Messaging works."
        try:
            message_id = channels[channel].send(chat_id, text)
        except ChannelError as exc:
            print(f"Send over {channel} failed: {exc}", file=sys.stderr)
            return 1
        finally:
            for ch in channels.values():
                ch.close()
        print(f"Sent over {channel} to {chat_id} (message {message_id}).")
        return 0

    if args.action == "whoami":
        if not config.telegram_bot_token:
            print("TELEGRAM_BOT_TOKEN is not set; whoami only works for Telegram.", file=sys.stderr)
            return 2
        from assistant.channels.telegram import TelegramChannel

        seconds = args.seconds
        print(f"Send /start (or any message) to your bot now; listening for {seconds} seconds...")
        tg = TelegramChannel(config.telegram_bot_token, set(config.telegram_chat_ids), store)
        try:
            tg.start()
            seen = tg.whoami(seconds)
        except ChannelError as exc:
            print(f"Telegram error: {exc}", file=sys.stderr)
            return 1
        finally:
            tg.close()
        if not seen:
            print("No messages arrived. Check the token and try again.")
            return 1
        ids: dict[str, str] = {}
        for item in seen:
            ids.setdefault(item["chat_id"], item["sender"])
        for chat_id, sender in ids.items():
            mark = " (allowlisted)" if chat_id in config.telegram_chat_ids else ""
            print(f"chat id {chat_id}  from {sender or '?'}{mark}")
        if any(c not in config.telegram_chat_ids for c in ids):
            print("Put your chat id in TELEGRAM_CHAT_ID in .env and restart the daemon.")
        return 0
    return 1


# ------------------------------------------------------------- schedule
def _fmt_schedule(row: dict[str, Any], config: Config) -> str:
    when = format_local(row["next_run_utc"], config.tz)
    repeat = f"  every {row['repeat']}" if row.get("repeat") else ""
    text = row["text"] or f"({row['kind']})"
    if row.get("kind") == "call":
        text = f"[call] {text}"
    priority = row.get("priority") or "normal"
    pri = f"  !{priority}" if priority != "normal" and row.get("kind") != "call" else ""
    state = "" if row.get("enabled") else "  [cancelled]"
    source = f"  <{row['source']}>" if row.get("source") else ""
    return f"#{row['id']} {when}{repeat}  {row['channel']} {row['chat_id']}  {text}{pri}{source}{state}"


def _fmt_delivery(d: dict[str, Any], config: Config) -> str:
    when = format_local(d["fire_at_utc"], config.tz)
    extra = f"  {d['error']}" if d.get("error") else ""
    return f"{when}  #{d['schedule_id']} {d.get('kind') or '?'}  {d['status']}{extra}"


def cmd_schedule(args: argparse.Namespace, store: Store, config: Config) -> int:
    if args.action == "list":
        rows = store.list_schedules(enabled_only=not args.all, include_system=args.all)
        _print_list("Scheduled messages", rows, lambda r: _fmt_schedule(r, config))
        return 0
    if args.action == "add":
        from assistant.tools import ToolContext, run_tool

        if config.default_target() is None:
            print(_NO_CHANNELS_MESSAGE, file=sys.stderr)
            return 2
        ctx = ToolContext(store, tz=config.tz, config=config)
        out, err = run_tool(
            ctx,
            "schedule_message",
            {"text": args.text, "when": args.at, "repeat": args.repeat, "priority": args.priority},
        )
        if err:
            print(out, file=sys.stderr)
            return 1
        view = json.loads(out)
        repeat = f", repeating {view['repeat']}" if view.get("repeat") else ""
        priority = f" ({view['priority']})" if view.get("priority", "normal") != "normal" else ""
        print(f"Scheduled #{view['id']}: \"{view['text']}\" at {view['next_run']}{repeat} via {view['channel']}{priority}.")
        if view.get("note"):
            print(view["note"])
        return 0
    if args.action == "cancel":
        row = store.get_schedule(args.id)
        if row is None:
            print(f"No scheduled message with id {args.id}.", file=sys.stderr)
            return 1
        if store.cancel_schedule(args.id):
            print(f"Cancelled #{args.id}: {row['text'] or row['kind']}")
            return 0
        print(f"#{args.id} was already cancelled.")
        return 0
    if args.action == "sync":
        from assistant.scheduler import Scheduler

        def no_send(channel: str, chat_id: str, text: str) -> str:
            raise RuntimeError("sync does not send")

        scheduler = Scheduler(store, config, send=no_send, render_briefing=lambda *a: "", google=load_google(config))
        scheduler.ensure_fixed_schedules()
        scheduler.sync_auto_reminders()
        rows = store.list_schedules(enabled_only=True, include_system=True)
        auto = [r for r in rows if (r.get("source") or "").startswith(("task:", "event:", "gcal:"))]
        fixed = [r for r in rows if (r.get("source") or "").startswith("system:")]
        print(f"{len(fixed)} fixed (briefing/review), {len(auto)} automatic reminders, {len(rows)} active in total.")
        return 0
    if args.action == "deliveries":
        _print_list("Deliveries", store.list_deliveries(args.limit), lambda d: _fmt_delivery(d, config))
        return 0
    return 1


# ----------------------------------------------------------------- demo
def cmd_demo(args: argparse.Namespace, store: Store, config: Config) -> int:
    import dataclasses

    from assistant.channels.console import CHAT_ID, ConsoleChannel, ConsoleVoice
    from assistant.daemon import Daemon
    from assistant.demo import BANNER, ScriptedClient

    demo_config = dataclasses.replace(
        config,
        data_dir=config.data_dir / "demo",
        tick_seconds=5,
        google=False,
        default_channel=None,
        telegram_bot_token="",
        telegram_chat_ids=[],
        twilio_account_sid="",
        twilio_auth_token="",
        user_phone=config.user_phone or "your phone",  # calls are printed, never dialled
    )
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    console = ConsoleChannel()
    daemon = Daemon(
        demo_config,
        client=ScriptedClient(demo_config.tz),
        channels={"console": console},
        voice=ConsoleVoice(prompt=console.prompt),
    )
    console.on_eof = daemon.stop

    def after_turn(msg: Any, result: Any) -> None:
        if any(call["name"] == "schedule_message" and not call["error"] for call in result.tool_calls):
            rows = daemon._store().list_schedules(enabled_only=True, include_system=False)
            print("\n  schedule list:")
            for row in rows:
                print("    " + _fmt_schedule(row, demo_config))
            print(f"  (the scheduler ticks every {demo_config.tick_seconds}s; watch it fire)")
            sys.stdout.write(console.prompt)
            sys.stdout.flush()

    daemon.on_turn = after_turn
    print(BANNER)
    print(f"Scratch database: {demo_config.db_path}\n")
    briefing = daemon.render_briefing("briefing", "console", CHAT_ID)
    console.send(CHAT_ID, briefing)
    return daemon.run()


# ---------------------------------------------------------------- setup
def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def write_env(path: Path, updates: dict[str, str], confirm=None) -> list[str]:
    """Merge `updates` into the .env at `path`, keeping comments and order.

    An existing, different, non-empty value is only replaced when `confirm(key)`
    returns True. Returns the keys that were written.
    """
    existing = _read_env_file(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    written: list[str] = []
    for key, value in updates.items():
        if key in existing and existing[key] and existing[key] != value:
            if confirm is None or not confirm(key):
                continue
        replaced = False
        for i, raw in enumerate(lines):
            stripped = raw.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            if stripped.partition("=")[0].strip() == key:
                lines[i] = f"{key}={value}"
                replaced = True
                break
        if not replaced:
            lines.append(f"{key}={value}")
        written.append(key)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return written


def _ask(prompt: str, default: str = "", secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        if secret and sys.stdin.isatty():
            import getpass

            value = getpass.getpass(f"{prompt}{suffix}: ")
        else:
            value = input(f"{prompt}{suffix}: ")
    except EOFError:
        return default
    value = value.strip()
    return value or default


def _yes(prompt: str, default: bool = True) -> bool:
    answer = _ask(prompt + (" [Y/n]" if default else " [y/N]"))
    if not answer:
        return default
    return answer.lower().startswith("y")


def _validate_telegram_token(token: str) -> tuple[bool | None, str]:
    """(True, bot username) / (False, reason) / (None, reason) when offline."""
    import httpx2

    try:
        resp = httpx2.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        data = resp.json()
    except (httpx2.HTTPError, ValueError) as exc:
        return None, f"could not reach Telegram ({exc.__class__.__name__}); skipping validation"
    if data.get("ok"):
        return True, data.get("result", {}).get("username", "")
    return False, data.get("description") or f"HTTP {resp.status_code}"


def cmd_setup(args: argparse.Namespace, store: Store, config: Config) -> int:
    env_path = Path(args.env)
    current = _read_env_file(env_path)
    if env_path.is_file():
        print(f"Updating {env_path} (existing values are kept unless you confirm a change).")
    else:
        print(f"Creating {env_path}.")
    updates: dict[str, str] = {}

    print("\n1. Anthropic API key (https://console.anthropic.com/)")
    key = _ask("ANTHROPIC_API_KEY", current.get("ANTHROPIC_API_KEY", ""), secret=True)
    if key and key != "sk-ant-...":
        updates["ANTHROPIC_API_KEY"] = key
    else:
        print("   Skipped; the assistant will only run `demo` until a key is set.")

    print("\n2. Telegram bot token (message @BotFather, send /newbot, copy the token)")
    token = _ask("TELEGRAM_BOT_TOKEN", current.get("TELEGRAM_BOT_TOKEN", ""), secret=True)
    if token:
        ok, info = _validate_telegram_token(token)
        if ok is True:
            print(f"   Token works: @{info}")
        elif ok is False:
            print(f"   Telegram rejected the token: {info}")
            if not _yes("   Keep it anyway?", default=False):
                token = ""
        else:
            print(f"   {info}")
        if token:
            updates["TELEGRAM_BOT_TOKEN"] = token

    chat_id = current.get("TELEGRAM_CHAT_ID", "")
    if token:
        print("\n3. Your chat id (send /start to your bot so it can be captured)")
        if ok is not None and _yes(f"   Listen for 60 seconds now?{' (current: ' + chat_id + ')' if chat_id else ''}"):
            from assistant.channels import ChannelError
            from assistant.channels.telegram import TelegramChannel

            tg = TelegramChannel(token, set(), store)
            try:
                tg.start()
                print("   Waiting... send /start to the bot from your phone.")
                seen = tg.whoami(60)
            except ChannelError as exc:
                seen = []
                print(f"   Telegram error: {exc}")
            finally:
                tg.close()
            if seen:
                chat_id = seen[0]["chat_id"]
                print(f"   Captured chat id {chat_id} (from {seen[0]['sender'] or '?'}).")
            else:
                print("   Nothing arrived; run `my-assistant channels whoami` later.")
        chat_id = _ask("TELEGRAM_CHAT_ID", chat_id)
        if chat_id:
            updates["TELEGRAM_CHAT_ID"] = chat_id

    print("\n4. Phone calls (optional)")
    print(
        "   With a Twilio account the assistant can call your phone for things that matter\n"
        "   (\"call me if...\", flights, medication). A Twilio number costs about $1.15/month\n"
        "   and calls about $0.015/minute. Sign up at https://www.twilio.com/ and buy a\n"
        "   voice-capable number; on a trial account, verify your own number first."
    )
    if _yes("   Set up phone calls now?", default=bool(current.get("TWILIO_ACCOUNT_SID"))):
        sid = _ask("TWILIO_ACCOUNT_SID", current.get("TWILIO_ACCOUNT_SID", ""))
        auth = _ask("TWILIO_AUTH_TOKEN", current.get("TWILIO_AUTH_TOKEN", ""), secret=True)
        voice_from = _ask(
            "Twilio number to call from, E.164 (TWILIO_VOICE_FROM)",
            current.get("TWILIO_VOICE_FROM") or current.get("TWILIO_FROM", ""),
        )
        phone = _ask("Your phone number, E.164 (USER_PHONE)", current.get("USER_PHONE", ""))
        for env_key, value in (
            ("TWILIO_ACCOUNT_SID", sid),
            ("TWILIO_AUTH_TOKEN", auth),
            ("TWILIO_VOICE_FROM", voice_from),
            ("USER_PHONE", phone),
        ):
            if value:
                updates[env_key] = value
        if sid and auth and voice_from and phone:
            print("   Calls are on. Try: my-assistant channels test --call")
        else:
            print("   Incomplete; calls stay off until all four values are set.")
    else:
        print("   Skipped; the assistant will text only.")

    print("\n5. Preferences")
    name = _ask("Your name (ASSISTANT_USER_NAME)", current.get("ASSISTANT_USER_NAME", ""))
    if name:
        updates["ASSISTANT_USER_NAME"] = name
    while True:
        tz_name = _ask("Timezone (IANA)", current.get("ASSISTANT_TIMEZONE", DEFAULT_TIMEZONE))
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(tz_name)
            break
        except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError or ValueError
            print(f"   {tz_name!r} is not a known timezone (example: America/Toronto, Europe/London).")
    updates["ASSISTANT_TIMEZONE"] = tz_name
    for env_key, label, default in (
        ("ASSISTANT_MORNING_BRIEFING", "Morning briefing time (HH:MM, empty to disable)", "07:30"),
        ("ASSISTANT_EVENING_REVIEW", "Evening review time (HH:MM, empty to disable)", "21:00"),
    ):
        while True:
            value = _ask(label, current.get(env_key, default))
            if value in {"", "off", "none"}:
                value = ""
                break
            try:
                value = validate_hhmm(value)
                break
            except ValueError as exc:
                print(f"   {exc}")
        updates[env_key] = value

    def confirm(k: str) -> bool:
        return _yes(f"{k} is already set in {env_path.name}; replace it?", default=False)

    written = write_env(env_path, updates, confirm=confirm)
    print(f"\nWrote {', '.join(written) if written else 'nothing new'} to {env_path}.")
    if "ANTHROPIC_API_KEY" not in {**current, **updates} or not {**current, **updates}.get("ANTHROPIC_API_KEY"):
        print("Try it without a key: my-assistant demo")
    if updates.get("TELEGRAM_CHAT_ID") or current.get("TELEGRAM_CHAT_ID"):
        print("Next: my-assistant channels test, then my-assistant daemon")
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
    p.add_argument("--send", action="store_true", help="text it to you instead of printing only")
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

    p = sub.add_parser("google", help="connect Google Calendar and Gmail")
    p.add_argument("action", choices=["login", "status", "logout"])
    p.set_defaults(func=cmd_google)

    p = sub.add_parser("serve", help="run the web UI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("daemon", help="run the always-on assistant (messaging + scheduled texts)")
    p.add_argument("--once", action="store_true", help="run one scheduler pass and exit")
    p.set_defaults(func=cmd_daemon)

    p = sub.add_parser("channels", help="inspect and test messaging channels")
    ch = p.add_subparsers(dest="action", required=True)
    ch.add_parser("status", help="what is configured")
    q = ch.add_parser("test", help="send yourself a test message (or a test call)")
    q.add_argument("--text", default="")
    q.add_argument("--call", action="store_true", help="place a test phone call instead of sending a text")
    q = ch.add_parser("whoami", help="discover your Telegram chat id")
    q.add_argument("--seconds", type=int, default=60)
    p.set_defaults(func=cmd_channels)

    p = sub.add_parser("schedule", help="manage scheduled texts (no API calls)")
    sc = p.add_subparsers(dest="action", required=True)
    q = sc.add_parser("list", help="upcoming scheduled messages")
    q.add_argument("--all", action="store_true", help="include cancelled and automatic reminders")
    q = sc.add_parser("add", help="schedule a text")
    q.add_argument("text")
    q.add_argument("--at", required=True, help="local time, e.g. 2026-09-12T15:00")
    q.add_argument("--repeat", default=None, help="daily | weekdays | weekends | weekly | days:mon,wed")
    q.add_argument(
        "--priority",
        choices=["normal", "important", "critical"],
        default="normal",
        help="important: text, then call if you don't reply; critical: call and text at once",
    )
    q = sc.add_parser("cancel", help="cancel a scheduled message")
    q.add_argument("id", type=int)
    sc.add_parser("sync", help="rebuild briefing and task/event reminder rows now")
    q = sc.add_parser("deliveries", help="what was sent, skipped, or failed")
    q.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_schedule)

    p = sub.add_parser("demo", help="scripted end-to-end demo, no credentials needed")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("setup", help="interactive setup wizard that writes .env")
    p.add_argument("--env", default=".env", help="path of the .env file to write")
    p.set_defaults(func=cmd_setup)

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
