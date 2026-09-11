"""Messaging channels the daemon can talk over."""

from __future__ import annotations

from assistant.channels.base import (
    Channel,
    ChannelError,
    ConflictError,
    InboundMessage,
    split_message,
)
from assistant.config import Config
from assistant.store import Store


def build_channels(config: Config, store: Store) -> dict[str, Channel]:
    """Instantiate every channel the configuration has credentials for."""
    channels: dict[str, Channel] = {}
    if config.telegram_bot_token:
        from assistant.channels.telegram import TelegramChannel

        channels["telegram"] = TelegramChannel(
            config.telegram_bot_token, set(config.telegram_chat_ids), store
        )
    if config.twilio_account_sid and config.twilio_auth_token:
        from assistant.channels.twilio import TwilioChannel

        channels["twilio"] = TwilioChannel(
            config.twilio_account_sid,
            config.twilio_auth_token,
            config.twilio_from,
            config.twilio_whatsapp_from,
            set(config.allowed_chat_ids("twilio")),
            config.twilio_webhook_url,
            store,
        )
    return channels


__all__ = [
    "Channel",
    "ChannelError",
    "ConflictError",
    "InboundMessage",
    "build_channels",
    "split_message",
]
