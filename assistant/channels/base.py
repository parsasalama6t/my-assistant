"""Common types for messaging channels (Telegram, Twilio, console)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class InboundMessage:
    channel: str
    chat_id: str
    text: str
    message_id: str
    sender: str = ""
    received_at: datetime = field(default_factory=_utc_now)


class ChannelError(RuntimeError):
    """A provider call failed; the message carries the provider's explanation."""


class ConflictError(ChannelError):
    """Another process is already receiving messages for this bot."""


class Channel:
    """Base class: outbound `send`, optional inbound `poll`, and an allowlist check.

    `inbound_deduped` is True when the channel already drops repeated inbound
    messages itself (Twilio does, in its webhook handler), so the daemon does
    not record the message id a second time.
    """

    name: str = ""
    max_len: int = 4096
    inbound_deduped: bool = False

    def send(self, chat_id: str, text: str) -> str:
        """Deliver `text` to `chat_id` and return the provider's message id."""
        raise NotImplementedError

    def poll(self, timeout: float = 25) -> list[InboundMessage]:
        """Block up to `timeout` seconds and return new inbound messages."""
        return []

    def is_allowed(self, chat_id: str) -> bool:
        return True

    def typing(self, chat_id: str) -> None:
        """Best-effort 'typing...' indicator; channels without one do nothing."""

    def close(self) -> None:
        return None


def split_message(text: str, limit: int) -> list[str]:
    """Split `text` into chunks of at most `limit` characters.

    Prefers paragraph breaks, then line breaks, then spaces; falls back to a
    hard cut when a single word is longer than the limit.
    """
    text = (text or "").strip()
    if not text:
        return []
    if limit <= 0 or len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = -1
        sep_len = 0
        for sep in ("\n\n", "\n", " "):
            idx = rest.rfind(sep, 0, limit + 1)
            if idx > 0:
                cut, sep_len = idx, len(sep)
                break
        if cut <= 0:
            cut, sep_len = limit, 0
        chunk = rest[:cut].rstrip()
        rest = rest[cut + sep_len :].lstrip()
        if chunk:
            chunks.append(chunk)
    if rest:
        chunks.append(rest)
    return chunks
