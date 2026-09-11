from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

import pytest
from anthropic.types import Message, TextBlock, ToolUseBlock, Usage

from assistant.config import Config
from assistant.store import Store


@pytest.fixture
def store() -> Store:
    s = Store(":memory:")
    yield s
    s.close()


@pytest.fixture
def config() -> Config:
    return Config(model="claude-opus-5", effort="medium", user_name="Sam", fallbacks=True)


def make_message(blocks: list[Any], stop_reason: str = "end_turn") -> Message:
    return Message(
        id="msg_test",
        type="message",
        role="assistant",
        model="claude-opus-5",
        content=blocks,
        stop_reason=stop_reason,
        stop_sequence=None,
        usage=Usage(input_tokens=10, output_tokens=5),
    )


def text(s: str) -> TextBlock:
    return TextBlock(type="text", text=s)


def tool_use(name: str, tool_input: dict[str, Any], tool_id: str = "toolu_1") -> ToolUseBlock:
    return ToolUseBlock(type="tool_use", id=tool_id, name=name, input=tool_input)


class FakeStream:
    """Mimics the SDK's MessageStreamManager / MessageStream for one response."""

    def __init__(self, message: Message) -> None:
        self.message = message

    def __enter__(self) -> "FakeStream":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def __iter__(self):
        for block in self.message.content:
            if block.type == "text":
                yield SimpleNamespace(
                    type="content_block_delta",
                    delta=SimpleNamespace(type="text_delta", text=block.text),
                )

    def get_final_message(self) -> Message:
        return self.message


class FakeMessages:
    def __init__(self, responses: list[Message], calls: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.calls = calls

    def stream(self, **kwargs: Any) -> FakeStream:
        self.calls.append(copy.deepcopy(kwargs))  # snapshot: the agent mutates `messages` later
        if not self._responses:
            raise AssertionError("no scripted responses left")
        return FakeStream(self._responses.pop(0))


class FakeClient:
    """Scripted stand-in for anthropic.Anthropic: returns responses in order."""

    def __init__(self, responses: list[Message]) -> None:
        self.calls: list[dict[str, Any]] = []
        self.messages = FakeMessages(responses, self.calls)
        self.beta = SimpleNamespace(messages=self.messages)
