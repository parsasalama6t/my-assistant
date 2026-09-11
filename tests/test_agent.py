from __future__ import annotations

import pytest

from assistant.agent import FALLBACK_BETA, Assistant, AssistantError
from assistant.config import Config
from assistant.prompts import build_system
from assistant.store import Store
from tests.conftest import FakeClient, make_message, text, tool_use


def test_plain_reply_is_streamed_and_persisted(store: Store, config: Config) -> None:
    client = FakeClient([make_message([text("Hello Sam!")])])
    a = Assistant(store, config, client=client)
    chunks: list[str] = []
    result = a.chat("hi", on_text=chunks.append)

    assert result.text == "Hello Sam!" and chunks == ["Hello Sam!"]
    assert result.stop_reason == "end_turn" and result.tool_calls == []
    history = store.get_messages(a.session_id)
    assert history[0] == {"role": "user", "content": "hi"}
    assert history[1]["role"] == "assistant"
    assert history[1]["content"][0]["text"] == "Hello Sam!"


def test_request_shape_uses_fallbacks_for_opus_5(store: Store, config: Config) -> None:
    client = FakeClient([make_message([text("ok")])])
    Assistant(store, config, client=client).chat("hi")
    call = client.calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["betas"] == [FALLBACK_BETA] and call["fallbacks"] == "default"
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"] == {"effort": "medium"}
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert any(t["name"] == "add_task" for t in call["tools"])


def test_no_fallbacks_when_disabled(store: Store) -> None:
    config = Config(fallbacks=False)
    client = FakeClient([make_message([text("ok")])])
    Assistant(store, config, client=client).chat("hi")
    assert "fallbacks" not in client.calls[0]


def test_tool_loop_executes_and_feeds_results_back(store: Store, config: Config) -> None:
    client = FakeClient(
        [
            make_message(
                [text("Adding that."), tool_use("add_task", {"title": "Buy milk", "due": "2026-09-12"})],
                stop_reason="tool_use",
            ),
            make_message([text("Done: Buy milk, due Sept 12.")]),
        ]
    )
    a = Assistant(store, config, client=client)
    seen: list[str] = []
    result = a.chat("remind me to buy milk on the 12th", on_tool=lambda n, i, o, e: seen.append(n))

    assert seen == ["add_task"]
    assert store.list_tasks()[0]["title"] == "Buy milk"
    assert result.text == "Adding that.Done: Buy milk, due Sept 12."
    assert result.tool_calls[0]["error"] is False

    # Second request carried the assistant tool_use turn plus a tool_result user turn.
    second = client.calls[1]["messages"]
    assert second[-2]["role"] == "assistant"
    assert second[-2]["content"][1]["type"] == "tool_use"
    assert second[-1]["role"] == "user"
    assert second[-1]["content"][0]["type"] == "tool_result"
    assert second[-1]["content"][0]["tool_use_id"] == "toolu_1"
    assert "is_error" not in second[-1]["content"][0]


def test_tool_error_is_returned_as_is_error(store: Store, config: Config) -> None:
    client = FakeClient(
        [
            make_message([tool_use("delete_task", {"id": 42})], stop_reason="tool_use"),
            make_message([text("That task doesn't exist.")]),
        ]
    )
    Assistant(store, config, client=client).chat("delete task 42")
    tool_result = client.calls[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True and "no task" in tool_result["content"]


def test_history_is_replayed_across_turns(store: Store, config: Config) -> None:
    client = FakeClient([make_message([text("one")]), make_message([text("two")])])
    a = Assistant(store, config, client=client)
    a.chat("first")
    a.chat("second")
    msgs = client.calls[1]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[0]["content"] == "first" and msgs[2]["content"] == "second"


def test_memories_appear_in_system_prompt(store: Store, config: Config) -> None:
    store.add_memory("Allergic to peanuts", "health")
    client = FakeClient([make_message([text("noted")])])
    Assistant(store, config, client=client).chat("hi")
    system_text = client.calls[0]["system"][1]["text"]
    assert "Allergic to peanuts" in system_text and "Sam" in system_text


def test_refusal_is_surfaced(store: Store, config: Config) -> None:
    client = FakeClient([make_message([], stop_reason="refusal")])
    result = Assistant(store, config, client=client).chat("hi")
    assert result.stop_reason == "refusal" and result.refusal and result.text == result.refusal


def test_empty_message_rejected(store: Store, config: Config) -> None:
    with pytest.raises(AssistantError):
        Assistant(store, config, client=FakeClient([])).chat("   ")


def test_new_session_resets_history(store: Store, config: Config) -> None:
    client = FakeClient([make_message([text("a")]), make_message([text("b")])])
    a = Assistant(store, config, client=client)
    a.chat("first")
    a.new_session()
    a.chat("second")
    assert [m["content"] for m in client.calls[1]["messages"]] == ["second"]


def test_build_system_without_memories() -> None:
    blocks = build_system("", [])
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert "saved memories" in blocks[1]["text"]


def test_missing_credentials_become_assistant_error(store: Store, config: Config) -> None:
    class NoAuthMessages:
        def stream(self, **kwargs):
            raise TypeError("Could not resolve authentication method. Expected one of api_key...")

    class NoAuthClient:
        messages = NoAuthMessages()
        beta = type("Beta", (), {"messages": NoAuthMessages()})()

    with pytest.raises(AssistantError, match="ANTHROPIC_API_KEY"):
        Assistant(store, config, client=NoAuthClient()).chat("hi")


def test_system_prompt_mentions_timezone_and_channel(store: Store) -> None:
    config = Config(user_name="Sam", timezone="America/Toronto", fallbacks=False)
    client = FakeClient([make_message([text("ok")])])
    Assistant(store, config, client=client, channel="telegram", chat_id="42").chat("hi")
    system_text = client.calls[0]["system"][1]["text"]
    assert "America/Toronto" in system_text
    assert "replying over Telegram" in system_text and "plain text" in system_text
    assert "schedule_message" in client.calls[0]["system"][0]["text"]


def test_no_channel_line_for_cli(store: Store, config: Config) -> None:
    client = FakeClient([make_message([text("ok")])])
    Assistant(store, config, client=client).chat("hi")
    assert "replying over" not in client.calls[0]["system"][1]["text"]


def test_build_system_channel_labels() -> None:
    assert "WhatsApp" in build_system("", [], channel="whatsapp")[1]["text"]
    assert "SMS" in build_system("", [], channel="sms")[1]["text"]
    assert "replying over" not in build_system("", [])[1]["text"]


def test_scheduling_tools_follow_messaging_config(store: Store) -> None:
    client = FakeClient([make_message([text("ok")])])
    Assistant(store, Config(fallbacks=False), client=client).chat("hi")
    assert "schedule_message" not in {t["name"] for t in client.calls[0]["tools"]}

    messaging = Config(fallbacks=False, default_channel="telegram", telegram_bot_token="t", telegram_chat_ids=["42"])
    client = FakeClient(
        [
            make_message(
                [tool_use("schedule_message", {"text": "Go to bed", "when": "2999-01-01T22:00"})],
                stop_reason="tool_use",
            ),
            make_message([text("Scheduled.")]),
        ]
    )
    a = Assistant(store, messaging, client=client, channel="telegram", chat_id="42")
    result = a.chat("text me at 10pm to go to bed")
    assert "schedule_message" in {t["name"] for t in client.calls[0]["tools"]}
    assert result.tool_calls[0]["error"] is False, result.tool_calls[0]["output"]
    row = store.list_schedules()[0]
    assert row["text"] == "Go to bed" and row["chat_id"] == "42" and row["tz"] == "America/Toronto"
    assert row["next_run_utc"] == "2999-01-02T03:00:00+00:00"  # 22:00 EST
    # The chat session is separate from the CLI's latest session.
    assert a.session_id != store.session_for_chat("telegram", "42", 3600)
