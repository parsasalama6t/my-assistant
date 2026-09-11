"""The always-on process: answers chat messages and sends scheduled texts.

Threads:
  * one poller per channel that supports `poll` (Telegram long polling),
  * a webhook listener when Twilio is configured with a public URL,
  * the scheduler loop,
  * a single worker that handles inbound messages one at a time.

Each thread opens its own SQLite connection through `store_factory` (the
database is in WAL mode), and every model turn runs under `turn_lock`.
"""

from __future__ import annotations

import logging
import queue
import signal
import threading
import traceback
from typing import Any, Callable
from urllib.parse import urlparse

from assistant.agent import Assistant, AssistantError, TurnResult
from assistant.channels import Channel, ChannelError, ConflictError, InboundMessage, build_channels, split_message
from assistant.config import Config
from assistant.prompts import BRIEFING_PROMPT, EVENING_REVIEW_PROMPT
from assistant.schedule_rules import format_local
from assistant.scheduler import Scheduler, TickReport
from assistant.store import Store

DEFAULT_WEBHOOK_PATH = "/webhooks/twilio"
POLL_TIMEOUT = 25
MAX_BACKOFF = 30

HELP_TEXT = """\
Commands:
/new - start a fresh conversation
/tasks - open tasks
/schedule - scheduled messages
/memories - what I remember about you
/id - show this chat's id
/help - this list
Anything else is answered by your assistant."""


class Daemon:
    def __init__(
        self,
        config: Config,
        store_factory: Callable[[], Store] | None = None,
        client: Any | None = None,
        google: Any | None = None,
        channels: dict[str, Channel] | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.store_factory = store_factory or (lambda: Store(config.db_path))
        self.client = client
        self.google = google
        self.log = log or logging.getLogger("assistant.daemon")
        self.stop_event = threading.Event()
        self.turn_lock = threading.Lock()
        self.queue: "queue.Queue[InboundMessage]" = queue.Queue()
        self.exit_code = 0
        self.on_turn: Callable[[InboundMessage, TurnResult], None] | None = None
        self._local = threading.local()
        self._stores: list[Store] = []
        self._stores_lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._webhook: Any | None = None
        if channels is None:
            channels = build_channels(config, self._store())
        self.channels: dict[str, Channel] = dict(channels)

    # ------------------------------------------------------------- stores
    def _store(self) -> Store:
        """This thread's Store, created on first use."""
        store = getattr(self._local, "store", None)
        if store is None:
            store = self.store_factory()
            self._local.store = store
            with self._stores_lock:
                if all(s is not store for s in self._stores):
                    self._stores.append(store)
        return store

    def _close_stores(self) -> None:
        with self._stores_lock:
            stores, self._stores = self._stores, []
        for store in stores:
            try:
                store.close()
            except Exception:  # noqa: BLE001
                pass

    @property
    def idle_seconds(self) -> int:
        return max(0, self.config.session_idle_hours) * 3600

    # ---------------------------------------------------------- messaging
    def send(self, channel: str, chat_id: str, text: str) -> str:
        ch = self.channels.get(channel)
        if ch is None:
            raise ChannelError(f"no channel named {channel!r} is configured")
        return ch.send(chat_id, text)

    def _reply(self, channel: str, chat_id: str, text: str) -> None:
        ch = self.channels.get(channel)
        limit = ch.max_len if ch is not None else 4096
        for chunk in split_message(text, limit) or [text]:
            self.send(channel, chat_id, chunk)

    def render_briefing(self, kind: str, channel: str, chat_id: str) -> str:
        prompt = EVENING_REVIEW_PROMPT if kind == "review" else BRIEFING_PROMPT
        store = self._store()
        session_id = store.session_for_chat(channel, chat_id, self.idle_seconds)
        with self.turn_lock:
            assistant = Assistant(
                store,
                self.config,
                client=self.client,
                session_id=session_id,
                google=self.google,
                channel=channel,
                chat_id=chat_id,
            )
            result = assistant.chat(prompt)
        return result.text or result.refusal or ""

    # ------------------------------------------------------------ inbound
    def _format_schedules(self, store: Store, channel: str, chat_id: str) -> str:
        rows = store.list_schedules(enabled_only=True, include_system=False)
        rows = [r for r in rows if r["channel"] == channel and r["chat_id"] == str(chat_id)] or rows
        if not rows:
            return "Nothing scheduled."
        lines = []
        for r in rows:
            when = format_local(r["next_run_utc"], self.config.tz)
            repeat = f" ({r['repeat']})" if r.get("repeat") else ""
            text = r["text"] or r["kind"]
            lines.append(f"#{r['id']} {when}{repeat}: {text}")
        return "Scheduled:\n" + "\n".join(lines)

    def _command(self, cmd: str, msg: InboundMessage, store: Store, allowed: bool) -> str | None:
        if cmd in {"/start", "/id"}:
            if allowed:
                return f"Your chat id is {msg.chat_id} (allowlisted; I will answer here)."
            return (
                f"Your chat id is {msg.chat_id} (not allowlisted). Add it to TELEGRAM_CHAT_ID "
                "in .env and restart the daemon."
            )
        if not allowed:
            return None
        if cmd == "/new":
            store.reset_chat_session(msg.channel, msg.chat_id)
            return "Started a new conversation."
        if cmd == "/help":
            return HELP_TEXT
        if cmd == "/tasks":
            tasks = store.list_tasks("open")
            if not tasks:
                return "No open tasks."
            lines = []
            for t in tasks:
                due = f" (due {t['due']})" if t.get("due") else ""
                pri = " !" if t.get("priority") == "high" else ""
                lines.append(f"- {t['title']}{due}{pri}")
            return "Open tasks:\n" + "\n".join(lines)
        if cmd == "/schedule":
            return self._format_schedules(store, msg.channel, msg.chat_id)
        if cmd == "/memories":
            memories = store.list_memories()
            if not memories:
                return "I don't have any saved memories yet."
            return "What I remember:\n" + "\n".join(f"- {m['content']}" for m in memories)
        return None

    def handle_inbound(self, msg: InboundMessage) -> str | None:
        """Answer one inbound message. Returns the reply text (None when dropped)."""
        channel = self.channels.get(msg.channel)
        if channel is None:
            self.log.warning("message from unknown channel %r dropped", msg.channel)
            return None
        store = self._store()
        if msg.message_id and not channel.inbound_deduped:
            if not store.record_inbound(msg.channel, msg.message_id):
                self.log.debug("duplicate %s message %s ignored", msg.channel, msg.message_id)
                return None
        allowed = channel.is_allowed(msg.chat_id)
        text = (msg.text or "").strip()
        if not text:
            return None

        if text.startswith("/"):
            cmd = text.split()[0].lower().split("@", 1)[0]
            reply = self._command(cmd, msg, store, allowed)
            if reply is not None:
                self._safe_reply(msg, reply)
                return reply
        if not allowed:
            self.log.info("ignoring message from %s chat %s (not allowlisted)", msg.channel, msg.chat_id)
            return None

        try:
            channel.typing(msg.chat_id)
        except Exception:  # noqa: BLE001 - purely cosmetic
            pass
        session_id = store.session_for_chat(msg.channel, msg.chat_id, self.idle_seconds)
        result: TurnResult | None = None
        try:
            with self.turn_lock:
                assistant = Assistant(
                    store,
                    self.config,
                    client=self.client,
                    session_id=session_id,
                    google=self.google,
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                )
                result = assistant.chat(text)
            reply = result.text or result.refusal or "(no reply)"
        except AssistantError as exc:
            reply = f"Sorry, something went wrong: {exc}"
        except Exception:  # noqa: BLE001 - keep the worker alive
            self.log.error("unexpected error handling message:\n%s", traceback.format_exc())
            reply = "Sorry, something went wrong on my side. Please try again in a moment."
        self._safe_reply(msg, reply)
        if result is not None and self.on_turn is not None:
            try:
                self.on_turn(msg, result)
            except Exception:  # noqa: BLE001
                self.log.exception("on_turn hook failed")
        return reply

    def _safe_reply(self, msg: InboundMessage, reply: str) -> None:
        try:
            self._reply(msg.channel, msg.chat_id, reply)
        except ChannelError as exc:
            self.log.warning("could not reply on %s to %s: %s", msg.channel, msg.chat_id, exc)

    # ------------------------------------------------------------ threads
    def _poll_loop(self, channel: Channel) -> None:
        start = getattr(channel, "start", None)
        if callable(start):
            try:
                start()
            except Exception as exc:  # noqa: BLE001
                self.log.warning("%s start failed: %s", channel.name, exc)
        self.log.info("polling %s", channel.name)
        backoff = 1
        while not self.stop_event.is_set():
            try:
                messages = channel.poll(POLL_TIMEOUT)
            except ConflictError as exc:
                self.log.error("%s: %s; stopping", channel.name, exc)
                self.exit_code = 1
                self.stop_event.set()
                return
            except ChannelError as exc:
                self.log.warning("%s poll failed: %s; retrying in %ds", channel.name, exc, backoff)
                self.stop_event.wait(backoff)
                backoff = min(MAX_BACKOFF, backoff * 2)
                continue
            except Exception:  # noqa: BLE001
                self.log.exception("%s poll crashed; retrying in %ds", channel.name, backoff)
                self.stop_event.wait(backoff)
                backoff = min(MAX_BACKOFF, backoff * 2)
                continue
            backoff = 1
            for msg in messages:
                self.queue.put(msg)

    def _worker_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                msg = self.queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self.handle_inbound(msg)
            except Exception:  # noqa: BLE001
                self.log.exception("worker failed on a message")

    def _scheduler_loop(self) -> None:
        scheduler = Scheduler(
            self._store(),
            self.config,
            send=self.send,
            render_briefing=self.render_briefing,
            google=self.google,
            log=self.log.getChild("scheduler"),
        )
        scheduler.run_forever(self.stop_event)

    def _start_webhook(self) -> None:
        twilio = self.channels.get("twilio")
        if twilio is None:
            return
        url = self.config.twilio_webhook_url
        if not url:
            self.log.warning(
                "twilio is configured but TWILIO_WEBHOOK_URL is empty; inbound WhatsApp/SMS "
                "messages will not be received (outbound still works)"
            )
            return
        from assistant.channels.webhook import WebhookServer

        path = urlparse(url).path or DEFAULT_WEBHOOK_PATH
        self._webhook = WebhookServer(
            self.config.twilio_webhook_port,
            path,
            url,
            handler=twilio.handle_webhook,  # type: ignore[attr-defined]
            on_message=self.queue.put,
            log=self.log.getChild("webhook"),
        )
        self._webhook.start()
        self.log.info("webhook listening on port %d", self._webhook.port)

    def _spawn(self, name: str, target: Callable[[], None]) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def stop(self) -> None:
        self.stop_event.set()

    def _install_signal_handlers(self) -> None:
        def handler(signum: int, frame: Any) -> None:
            self.log.info("received signal %s; shutting down", signum)
            self.stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass  # not the main thread (tests) or unsupported platform

    # --------------------------------------------------------------- runs
    def run_once(self) -> TickReport:
        """Sync reminders and run a single scheduler tick without starting threads."""
        scheduler = Scheduler(
            self._store(),
            self.config,
            send=self.send,
            render_briefing=self.render_briefing,
            google=self.google,
            log=self.log.getChild("scheduler"),
        )
        scheduler.ensure_fixed_schedules()
        return scheduler.run_once()

    def close(self) -> None:
        """Release channel clients and database connections (after `run_once`)."""
        for channel in self.channels.values():
            try:
                channel.close()
            except Exception:  # noqa: BLE001
                pass
        self._close_stores()

    def run(self) -> int:
        """Run until SIGINT/SIGTERM (or a poll conflict). Returns the exit code."""
        if not self.channels:
            raise ChannelError("no messaging channel is configured")
        self._install_signal_handlers()
        for channel in self.channels.values():
            if type(channel).poll is not Channel.poll:
                self._spawn(f"poll-{channel.name}", lambda ch=channel: self._poll_loop(ch))
        self._start_webhook()
        self._spawn("scheduler", self._scheduler_loop)
        self._spawn("worker", self._worker_loop)
        self.log.info("daemon started (channels: %s)", ", ".join(sorted(self.channels)))
        try:
            while not self.stop_event.wait(1.0):
                pass
        except KeyboardInterrupt:
            self.stop_event.set()
        self.log.info("stopping")
        if self._webhook is not None:
            self._webhook.stop()
        for thread in self._threads:
            thread.join(timeout=5)
        self.close()
        return self.exit_code
