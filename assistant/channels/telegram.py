"""Telegram Bot API channel: sendMessage for outbound, getUpdates long polling for inbound."""

from __future__ import annotations

import time
from typing import Any

import httpx2

from assistant.channels.base import (
    Channel,
    ChannelError,
    ConflictError,
    InboundMessage,
    split_message,
)
from assistant.store import Store

OFFSET_KEY = "telegram.offset"
MAX_LEN = 4096


class TelegramChannel(Channel):
    name = "telegram"
    max_len = MAX_LEN

    def __init__(
        self,
        token: str,
        allowed_chat_ids: set[str],
        store: Store,
        http: httpx2.Client | None = None,
        api_base: str = "https://api.telegram.org",
    ) -> None:
        if not token:
            raise ValueError("Telegram bot token is required")
        self.token = token
        self.allowed = {str(c) for c in allowed_chat_ids}
        self.store = store
        self.http = http or httpx2.Client(timeout=30)
        self.api_base = api_base.rstrip("/")

    # ------------------------------------------------------------ helpers
    def _url(self, method: str) -> str:
        return f"{self.api_base}/bot{self.token}/{method}"

    def _call(self, method: str, payload: dict[str, Any], read_timeout: float | None = None) -> Any:
        kwargs: dict[str, Any] = {"json": payload}
        if read_timeout is not None:
            kwargs["timeout"] = httpx2.Timeout(10.0, read=read_timeout)
        try:
            resp = self.http.post(self._url(method), **kwargs)
        except httpx2.HTTPError as exc:
            raise ChannelError(f"telegram {method}: {exc.__class__.__name__}: {exc}") from exc
        if resp.status_code == 409:
            raise ConflictError("another daemon is polling this bot")
        try:
            data = resp.json()
        except ValueError as exc:
            raise ChannelError(f"telegram {method}: HTTP {resp.status_code}") from exc
        if not isinstance(data, dict) or not data.get("ok"):
            desc = data.get("description") if isinstance(data, dict) else None
            raise ChannelError(desc or f"telegram {method}: HTTP {resp.status_code}")
        return data.get("result")

    def is_allowed(self, chat_id: str) -> bool:
        return str(chat_id) in self.allowed

    # ------------------------------------------------------------ outbound
    def send(self, chat_id: str, text: str) -> str:
        chunks = split_message(text, self.max_len) or [""]
        message_id = ""
        for chunk in chunks:
            result = self._call(
                "sendMessage",
                {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True},
            )
            message_id = str((result or {}).get("message_id", ""))
        return message_id

    def typing(self, chat_id: str) -> None:
        try:
            self._call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except ChannelError:
            pass

    def start(self) -> None:
        """Long polling only works without a webhook, so drop one if it exists."""
        try:
            self._call("deleteWebhook", {"drop_pending_updates": False})
        except ChannelError:
            pass

    # ------------------------------------------------------------- inbound
    def _fetch_updates(self, timeout: float) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": int(timeout), "allowed_updates": ["message"]}
        offset = self.store.get_state(OFFSET_KEY)
        if offset:
            payload["offset"] = int(offset)
        result = self._call("getUpdates", payload, read_timeout=timeout + 10)
        return list(result or [])

    @staticmethod
    def _to_message(update: dict[str, Any]) -> InboundMessage | None:
        message = update.get("message") or {}
        text = message.get("text")
        chat = message.get("chat") or {}
        if not text or "id" not in chat:
            return None
        sender = message.get("from") or {}
        return InboundMessage(
            channel="telegram",
            chat_id=str(chat["id"]),
            text=text,
            message_id=str(update.get("update_id")),
            sender=sender.get("username") or sender.get("first_name") or "",
        )

    def poll(self, timeout: float = 25) -> list[InboundMessage]:
        updates = self._fetch_updates(timeout)
        messages: list[InboundMessage] = []
        last_id: int | None = None
        for update in updates:
            if "update_id" in update:
                last_id = max(last_id or 0, int(update["update_id"]))
            msg = self._to_message(update)
            if msg is not None:
                messages.append(msg)
        if last_id is not None:
            self.store.set_state(OFFSET_KEY, str(last_id + 1))
        return messages

    def whoami(self, seconds: float = 60) -> list[dict[str, str]]:
        """Collect chat ids that message the bot for `seconds`, ignoring the allowlist."""
        seen: list[dict[str, str]] = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            remaining = max(1, min(25, int(deadline - time.monotonic())))
            for msg in self.poll(remaining):
                seen.append({"chat_id": msg.chat_id, "sender": msg.sender, "text": msg.text})
        return seen

    def close(self) -> None:
        self.http.close()
