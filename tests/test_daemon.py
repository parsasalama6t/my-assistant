from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from assistant.channels.base import Channel, ChannelError, InboundMessage
from assistant.config import Config
from assistant.daemon import Daemon
from assistant.scheduler import TickReport
from assistant.schedule_rules import to_utc_iso
from assistant.store import Store
from tests.conftest import FakeClient, make_message, text, tool_use
from tests.fakes import FakeVoice


class FakeChannel(Channel):
    name = "telegram"

    def __init__(self, allowed: set[str], max_len: int = 4096) -> None:
        self.allowed = allowed
        self.max_len = max_len
        self.sent: list[tuple[str, str]] = []
        self.typing_calls: list[str] = []

    def send(self, chat_id: str, text: str) -> str:
        self.sent.append((chat_id, text))
        return f"m{len(self.sent)}"

    def is_allowed(self, chat_id: str) -> bool:
        return chat_id in self.allowed

    def typing(self, chat_id: str) -> None:
        self.typing_calls.append(chat_id)


@pytest.fixture
def tg_config() -> Config:
    return Config(
        user_name="Sam",
        default_channel="telegram",
        telegram_bot_token="tok",
        telegram_chat_ids=["111"],
        timezone="America/Toronto",
    )


def make_daemon(
    store: Store, config: Config, responses: list, allowed={"111"}, max_len: int = 4096, voice=None
) -> tuple[Daemon, FakeChannel, FakeClient]:
    channel = FakeChannel(set(allowed), max_len=max_len)
    client = FakeClient(responses)
    daemon = Daemon(
        config,
        store_factory=lambda: store,
        client=client,
        channels={"telegram": channel},
        log=logging.getLogger("test.daemon"),
        voice=voice,
    )
    return daemon, channel, client


def inbound(text_: str, chat_id: str = "111", message_id: str = "1") -> InboundMessage:
    return InboundMessage(channel="telegram", chat_id=chat_id, text=text_, message_id=message_id)


def test_reply_is_sent_and_session_is_reused(store: Store, tg_config: Config) -> None:
    daemon, channel, client = make_daemon(
        store, tg_config, [make_message([text("Hi Sam!")]), make_message([text("Still here.")])]
    )
    assert daemon.handle_inbound(inbound("hello", message_id="1")) == "Hi Sam!"
    assert channel.sent == [("111", "Hi Sam!")]
    assert channel.typing_calls == ["111"]
    session = store.session_for_chat("telegram", "111", 3600)
    assert len(store.get_messages(session)) == 2
    assert store.get_messages(session)[0]["content"] == "hello"

    daemon.handle_inbound(inbound("again", message_id="2"))
    assert store.session_for_chat("telegram", "111", 3600) == session
    assert len(store.get_messages(session)) == 4
    assert len(client.calls[1]["messages"]) == 3  # history carried into the second turn
    assert client.calls[0]["tools"][-1]["name"] == "cancel_scheduled"  # scheduling tools are on

    # A replayed update id is ignored.
    assert daemon.handle_inbound(inbound("again", message_id="2")) is None
    assert len(channel.sent) == 2


def test_new_rotates_the_session_and_help_is_local(store: Store, tg_config: Config) -> None:
    daemon, channel, client = make_daemon(store, tg_config, [make_message([text("one")])])
    daemon.handle_inbound(inbound("first", message_id="1"))
    before = store.session_for_chat("telegram", "111", 3600)
    assert daemon.handle_inbound(inbound("/new", message_id="2")) == "Started a new conversation."
    after = store.session_for_chat("telegram", "111", 3600)
    assert after != before
    reply = daemon.handle_inbound(inbound("/help", message_id="3"))
    assert reply and "/schedule" in reply
    assert len(client.calls) == 1


def test_unknown_chat_gets_id_but_no_model_reply(store: Store, tg_config: Config) -> None:
    daemon, channel, client = make_daemon(store, tg_config, [make_message([text("nope")])])
    reply = daemon.handle_inbound(inbound("/id", chat_id="999", message_id="1"))
    assert reply and "999" in reply and "not allowlisted" in reply
    assert channel.sent == [("999", reply)]

    assert daemon.handle_inbound(inbound("hello?", chat_id="999", message_id="2")) is None
    assert len(channel.sent) == 1
    assert client.calls == []

    assert daemon.handle_inbound(inbound("/tasks", chat_id="999", message_id="3")) is None
    ok = daemon.handle_inbound(inbound("/start", chat_id="111", message_id="4"))
    assert ok and "111" in ok and "allowlisted" in ok and "not allowlisted" not in ok


def test_scripted_schedule_targets_the_inbound_chat(store: Store, tg_config: Config) -> None:
    when = (datetime.now(tg_config.tz) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
    daemon, channel, client = make_daemon(
        store,
        tg_config,
        [
            make_message([tool_use("schedule_message", {"text": "Leave now", "when": when})], stop_reason="tool_use"),
            make_message([text("Scheduled.")]),
        ],
    )
    assert daemon.handle_inbound(inbound("text me in two hours to leave")) == "Scheduled."
    rows = store.list_schedules()
    assert len(rows) == 1
    assert (rows[0]["channel"], rows[0]["chat_id"], rows[0]["text"]) == ("telegram", "111", "Leave now")

    listing = daemon.handle_inbound(inbound("/schedule", message_id="2"))
    assert listing and "Leave now" in listing and f"#{rows[0]['id']}" in listing


def test_assistant_error_becomes_an_apology(store: Store, tg_config: Config) -> None:
    class Broken:
        def __init__(self) -> None:
            self.messages = self
            self.beta = self

        def stream(self, **kwargs):
            import anthropic
            import httpx2

            request = httpx2.Request("POST", "https://api.example/v1/messages")
            raise anthropic.APIConnectionError(request=request)

    channel = FakeChannel({"111"})
    daemon = Daemon(tg_config, store_factory=lambda: store, client=Broken(), channels={"telegram": channel})
    reply = daemon.handle_inbound(inbound("hi"))
    assert reply and reply.startswith("Sorry, something went wrong")
    assert channel.sent[0][1] == reply


def test_long_replies_are_split_by_channel_limit(store: Store, tg_config: Config) -> None:
    long = " ".join(["word"] * 40)
    daemon, channel, client = make_daemon(store, tg_config, [make_message([text(long)])], max_len=50)
    daemon.handle_inbound(inbound("talk"))
    assert len(channel.sent) > 1
    assert all(len(t) <= 50 for _, t in channel.sent)
    assert " ".join(t for _, t in channel.sent) == long


def test_send_unknown_channel_raises(store: Store, tg_config: Config) -> None:
    daemon, channel, client = make_daemon(store, tg_config, [])
    with pytest.raises(ChannelError):
        daemon.send("twilio", "+1", "x")
    assert daemon.send("telegram", "111", "x") == "m1"


def test_run_once_returns_a_tick_report_and_render_briefing_sends_text(store: Store, tg_config: Config) -> None:
    daemon, channel, client = make_daemon(store, tg_config, [make_message([text("Good morning, Sam.")])])
    assert daemon.render_briefing("briefing", "telegram", "111") == "Good morning, Sam."
    assert client.calls[0]["messages"][0]["content"].startswith("Give me my briefing")

    client.messages._responses.append(make_message([text("Evening wrap-up.")]))
    store.add_schedule(
        kind="review",
        channel="telegram",
        chat_id="111",
        next_run_utc=to_utc_iso(datetime.now(timezone.utc) - timedelta(minutes=1)),
        tz="America/Toronto",
    )
    report = daemon.run_once()
    assert isinstance(report, TickReport)
    assert len(report.sent) == 1 and report.sent[0]["kind"] == "review"
    assert channel.sent[-1] == ("111", "Evening wrap-up.")
    # ensure_fixed_schedules ran as part of run_once
    sources = {r["source"] for r in store.list_schedules(include_system=True)}
    assert {"system:morning", "system:evening"} <= sources


class PollingChannel(FakeChannel):
    """Feeds scripted inbound messages through `poll`, then raises the given error."""

    def __init__(self, allowed: set[str], batches: list[list[InboundMessage]], then: Exception | None = None) -> None:
        super().__init__(allowed)
        self.batches = list(batches)
        self.then = then
        self.polls = 0

    def poll(self, timeout: float = 25) -> list[InboundMessage]:
        import time

        self.polls += 1
        if self.batches:
            return self.batches.pop(0)
        if self.then is not None and self.sent:  # raise once the worker has replied
            raise self.then
        time.sleep(0.05)
        return []


def test_run_answers_polled_messages_and_stops_on_conflict(tmp_path, tg_config: Config) -> None:
    import dataclasses
    import threading

    from assistant.channels.base import ConflictError

    config = dataclasses.replace(tg_config, data_dir=tmp_path, tick_seconds=1)
    channel = PollingChannel({"111"}, [[inbound("hello", message_id="7")]], then=ConflictError("another daemon is polling this bot"))
    client = FakeClient([make_message([text("Hi from the daemon.")])])
    daemon = Daemon(config, client=client, channels={"telegram": channel})

    codes: list[int] = []
    runner = threading.Thread(target=lambda: codes.append(daemon.run()), daemon=True)
    runner.start()
    runner.join(timeout=10)
    assert not runner.is_alive(), "daemon did not stop after the poll conflict"
    assert codes == [1]
    assert channel.sent == [("111", "Hi from the daemon.")]
    assert channel.polls >= 2

    store = Store(config.db_path)
    try:
        assert store.record_inbound("telegram", "7") is False  # the update id was recorded
        assert {r["source"] for r in store.list_schedules(include_system=True)} >= {"system:morning", "system:evening"}
    finally:
        store.close()


def test_run_refuses_without_channels(tg_config: Config, store: Store) -> None:
    daemon = Daemon(tg_config, store_factory=lambda: store, channels={})
    with pytest.raises(ChannelError):
        daemon.run()


# ---------------------------------------------------------------- voice
def _escalation(store: Store, chat_id: str, source: str) -> dict:
    return store.upsert_schedule_by_source(
        source, kind="call", text="Leave now", channel="telegram", chat_id=chat_id,
        next_run_utc=to_utc_iso(datetime.now(timezone.utc) + timedelta(minutes=5)),
        tz="America/Toronto", priority="critical",
    )


def test_inbound_reply_cancels_pending_escalations_for_that_chat(store: Store, tg_config: Config) -> None:
    tg_config.telegram_chat_ids = ["111", "222"]
    daemon, channel, client = make_daemon(
        store, tg_config, [make_message([text("Great, glad you saw it.")])], allowed={"111", "222"}
    )
    mine = _escalation(store, "111", "escalate:1:2026-09-11T12:00:00+00:00")
    mine2 = _escalation(store, "111", "escalate:2:2026-09-11T12:05:00+00:00")
    theirs = _escalation(store, "222", "escalate:3:2026-09-11T12:00:00+00:00")
    user_row = store.add_schedule("custom", "telegram", "111", mine["next_run_utc"], "America/Toronto", text="keep me")

    assert daemon.handle_inbound(inbound("on my way")) == "Great, glad you saw it."
    assert store.get_schedule(mine["id"])["enabled"] == 0
    assert store.get_schedule(mine2["id"])["enabled"] == 0
    assert store.get_schedule(theirs["id"])["enabled"] == 1  # other chat untouched
    assert store.get_schedule(user_row["id"])["enabled"] == 1  # only escalations are cancelled

    # A message from an unknown chat cancels nothing.
    assert daemon.handle_inbound(inbound("hello", chat_id="999", message_id="9")) is None
    assert store.get_schedule(theirs["id"])["enabled"] == 1
    # A local command counts as a reply too.
    daemon.handle_inbound(inbound("/tasks", chat_id="222", message_id="10"))
    assert store.get_schedule(theirs["id"])["enabled"] == 0


def test_call_me_tool_places_a_call_through_the_daemon(store: Store, tg_config: Config) -> None:
    tg_config.user_phone = "+14165550100"
    voice = FakeVoice()
    daemon, channel, client = make_daemon(
        store,
        tg_config,
        [
            make_message([tool_use("call_me", {"text": "The oven is still on."})], stop_reason="tool_use"),
            make_message([text("Calling you now.")]),
        ],
        voice=voice,
    )
    assert daemon.voice_enabled
    assert daemon.handle_inbound(inbound("call me and say the oven is on")) == "Calling you now."
    assert voice.calls == [("+14165550100", "The oven is still on.")]
    assert "call_me" in {t["name"] for t in client.calls[0]["tools"]}
    tool_results = client.calls[1]["messages"][-1]["content"]
    assert not tool_results[0].get("is_error") and "CA1" in tool_results[0]["content"]
    assert daemon.place_call("+14165550100", "again") == "CA2"
    daemon.close()
    assert voice.closed


def test_call_me_without_voice_is_a_tool_error_not_a_crash(store: Store, tg_config: Config) -> None:
    daemon, channel, client = make_daemon(
        store,
        tg_config,
        [
            make_message([tool_use("call_me", {"text": "hi"})], stop_reason="tool_use"),
            make_message([text("I can't call you from here.")]),
        ],
    )
    assert not daemon.voice_enabled
    assert daemon.handle_inbound(inbound("call me")) == "I can't call you from here."
    assert "call_me" not in {t["name"] for t in client.calls[0]["tools"]}
    tool_results = client.calls[1]["messages"][-1]["content"]
    assert tool_results[0].get("is_error") and "not set up" in tool_results[0]["content"]


def test_run_once_fires_a_critical_row_as_text_and_call(store: Store, tg_config: Config) -> None:
    tg_config.user_phone = "+14165550100"
    voice = FakeVoice()
    daemon, channel, client = make_daemon(store, tg_config, [], voice=voice)
    store.add_schedule(
        "custom", "telegram", "111", to_utc_iso(datetime.now(timezone.utc) - timedelta(minutes=1)),
        "America/Toronto", text="Gate closes in 10", priority="critical",
    )
    report = daemon.run_once()
    assert channel.sent == [("111", "Gate closes in 10")]
    assert voice.calls == [("+14165550100", "Gate closes in 10")]
    assert [s["via"] for s in report.sent] == ["text", "call"]


def test_daemon_records_last_inbound(store: Store, tg_config: Config) -> None:
    import json

    daemon, channel, client = make_daemon(store, tg_config, [])
    daemon.handle_inbound(InboundMessage(channel="telegram", chat_id="999", text="hello", message_id="m1", sender="Sam"))
    seen = json.loads(store.get_state("telegram.last_inbound"))
    assert seen["chat_id"] == "999" and seen["allowed"] is False and seen["text"] == "hello"
    assert channel.sent == [] and client.calls == []
