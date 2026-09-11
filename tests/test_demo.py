from __future__ import annotations

import io
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from assistant.channels.console import CHAT_ID, ConsoleChannel, ConsoleVoice
from assistant.config import Config
from assistant.daemon import Daemon
from assistant.demo import ScriptedClient
from assistant.store import Store

TORONTO = ZoneInfo("America/Toronto")


def _demo(store: Store) -> tuple[Daemon, ConsoleChannel, ConsoleVoice, io.StringIO]:
    out = io.StringIO()
    config = Config(timezone="America/Toronto", user_phone="your phone", tick_seconds=1)
    console = ConsoleChannel(stdin=io.StringIO(""), stdout=out, prompt="")
    voice = ConsoleVoice(stdout=out, prompt="")
    daemon = Daemon(
        config,
        store_factory=lambda: store,
        client=ScriptedClient(TORONTO),
        channels={"console": console},
        log=logging.getLogger("test.demo"),
        voice=voice,
    )
    return daemon, console, voice, out


def _msg(text: str, n: int):
    from assistant.channels.base import InboundMessage

    return InboundMessage(channel="console", chat_id=CHAT_ID, text=text, message_id=str(n))


def test_scripted_call_me_prints_a_call(store: Store) -> None:
    daemon, console, voice, out = _demo(store)
    reply = daemon.handle_inbound(_msg("call me and say the oven is on", 1))
    assert reply == "Calling you.\nCalling you now."
    assert voice.calls == [("your phone", "the oven is on")]
    assert "[assistant CALLS you]" in out.getvalue() and "the oven is on" in out.getvalue()


def test_scripted_timed_call_schedules_a_critical_row(store: Store) -> None:
    daemon, console, voice, out = _demo(store)
    reply = daemon.handle_inbound(_msg("call me in 3 minutes to leave", 1))
    assert reply and "call and text you" in reply and '"leave"' in reply
    rows = store.list_schedules()
    assert len(rows) == 1 and rows[0]["priority"] == "critical" and rows[0]["text"] == "leave"
    expected = (datetime.now(TORONTO) + timedelta(minutes=3)).strftime("%Y-%m-%d %H:%M")
    assert expected in reply
    assert voice.calls == []  # nothing rings until the row is due
    # "add task call the dentist" is still a task, not a call.
    daemon.handle_inbound(_msg("add task call the dentist tomorrow", 2))
    assert store.list_tasks()[0]["title"] == "call the dentist" and voice.calls == []
