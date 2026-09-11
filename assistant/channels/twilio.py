"""Twilio channel: SMS/WhatsApp via the REST API, inbound via a signed webhook."""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any, Mapping

import httpx2

from assistant.channels.base import Channel, ChannelError, InboundMessage, split_message
from assistant.store import Store

MAX_LEN = 1500
TWIML_EMPTY = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
WHATSAPP_WINDOW_CODE = 63016


def compute_signature(url: str, form: Mapping[str, str], auth_token: str) -> str:
    """Twilio request signature: HMAC-SHA1 over url + sorted key/value pairs, base64."""
    payload = url + "".join(f"{k}{form[k]}" for k in sorted(form))
    digest = hmac.new(auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def _strip_whatsapp(number: str) -> str:
    number = (number or "").strip()
    if number.lower().startswith("whatsapp:"):
        return number[len("whatsapp:") :]
    return number


class TwilioChannel(Channel):
    name = "twilio"
    max_len = MAX_LEN
    inbound_deduped = True

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        sms_from: str,
        whatsapp_from: str,
        allowed_numbers: set[str],
        webhook_url: str,
        store: Store,
        http: httpx2.Client | None = None,
        api_base: str = "https://api.twilio.com",
    ) -> None:
        if not (account_sid and auth_token):
            raise ValueError("Twilio account SID and auth token are required")
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.sms_from = sms_from
        self.whatsapp_from = whatsapp_from
        self.allowed = {_strip_whatsapp(n) for n in allowed_numbers if n}
        self.webhook_url = webhook_url
        self.store = store
        self.http = http or httpx2.Client(timeout=30)
        self.api_base = api_base.rstrip("/")
        self.auth = httpx2.BasicAuth(account_sid, auth_token)

    def is_allowed(self, chat_id: str) -> bool:
        return _strip_whatsapp(chat_id) in self.allowed

    # ------------------------------------------------------------ outbound
    def _sender_for(self, chat_id: str) -> str:
        if chat_id.lower().startswith("whatsapp:"):
            if not self.whatsapp_from:
                raise ChannelError("TWILIO_WHATSAPP_FROM is not set, so WhatsApp messages cannot be sent")
            return self.whatsapp_from
        if not self.sms_from:
            raise ChannelError("TWILIO_FROM is not set, so SMS messages cannot be sent")
        return self.sms_from

    def _explain(self, resp: httpx2.Response) -> str:
        try:
            data = resp.json()
        except ValueError:
            data = {}
        code = data.get("code") if isinstance(data, dict) else None
        message = (data.get("message") if isinstance(data, dict) else None) or f"HTTP {resp.status_code}"
        if code == WHATSAPP_WINDOW_CODE:
            return (
                "WhatsApp rejected the message (error 63016): free-form messages are only "
                "allowed within 24 hours of the user's last message. Text the bot first, or "
                "use an approved template."
            )
        return f"twilio error {code}: {message}" if code else f"twilio: {message}"

    def send(self, chat_id: str, text: str) -> str:
        url = f"{self.api_base}/2010-04-01/Accounts/{self.account_sid}/Messages.json"
        sender = self._sender_for(chat_id)
        sid = ""
        for chunk in split_message(text, self.max_len) or [""]:
            try:
                resp = self.http.post(
                    url, data={"To": chat_id, "From": sender, "Body": chunk}, auth=self.auth
                )
            except httpx2.HTTPError as exc:
                raise ChannelError(f"twilio: {exc.__class__.__name__}: {exc}") from exc
            if resp.status_code >= 400:
                raise ChannelError(self._explain(resp))
            try:
                sid = str(resp.json().get("sid", ""))
            except ValueError:
                sid = ""
        return sid

    # ------------------------------------------------------------- inbound
    def handle_webhook(
        self, url: str, headers: Mapping[str, str], form: Mapping[str, str]
    ) -> tuple[int, str, InboundMessage | None]:
        """Validate and translate one Twilio webhook POST.

        Returns (status, body, message). The body is always TwiML so Twilio does
        not send an automatic reply; `message` is None when nothing should be
        processed (bad signature, duplicate, or a sender not on the allowlist).
        """
        provided = ""
        for key, value in headers.items():
            if key.lower() == "x-twilio-signature":
                provided = value or ""
                break
        expected = compute_signature(url, form, self.auth_token)
        if not provided or not hmac.compare_digest(provided, expected):
            return 403, "invalid signature", None

        message_sid = form.get("MessageSid") or form.get("SmsSid") or ""
        if message_sid and not self.store.record_inbound("twilio", message_sid):
            return 200, TWIML_EMPTY, None
        sender = form.get("From") or ""
        if not sender or not self.is_allowed(sender):
            return 200, TWIML_EMPTY, None
        message = InboundMessage(
            channel="twilio",
            chat_id=sender,
            text=form.get("Body") or "",
            message_id=message_sid,
            sender=form.get("ProfileName") or sender,
        )
        return 200, TWIML_EMPTY, message

    def close(self) -> None:
        self.http.close()
