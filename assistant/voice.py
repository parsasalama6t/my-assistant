"""Outbound phone calls through Twilio Programmable Voice.

A call reads a short message aloud and hangs up. The TwiML script is passed
inline with the call request, so no public webhook is required.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from xml.sax.saxutils import escape

import httpx2 as httpx

if TYPE_CHECKING:
    from assistant.config import Config


class VoiceError(RuntimeError):
    """Raised when a call cannot be placed."""


def build_twiml(text: str, repeat: int = 2, voice: str = "Polly.Joanna") -> str:
    """TwiML that speaks `text` `repeat` times with a short pause between."""
    text = " ".join(text.split())
    if not text:
        raise ValueError("call text cannot be empty")
    say = f'<Say voice="{voice}">{escape(text)}</Say>'
    parts = [say]
    for _ in range(max(0, repeat - 1)):
        parts.append('<Pause length="1"/>')
        parts.append(say)
    return '<?xml version="1.0" encoding="UTF-8"?><Response>' + "".join(parts) + "</Response>"


class TwilioVoice:
    """Places calls with `POST /Accounts/{sid}/Calls.json`."""

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        from_number: str,
        allowed_numbers: set[str],
        http: httpx.Client | None = None,
        api_base: str = "https://api.twilio.com",
        voice: str = "Polly.Joanna",
    ) -> None:
        if not (account_sid and auth_token and from_number):
            raise ValueError("Twilio account SID, auth token, and a voice-capable number are required")
        self.account_sid = account_sid
        self.from_number = from_number
        self.allowed = {n.strip() for n in allowed_numbers if n.strip()}
        self.voice = voice
        self._url = f"{api_base}/2010-04-01/Accounts/{account_sid}/Calls.json"
        self._http = http or httpx.Client(auth=httpx.BasicAuth(account_sid, auth_token), timeout=20.0)

    def is_allowed(self, number: str) -> bool:
        return number.replace("whatsapp:", "") in self.allowed

    def call(self, to_number: str, text: str, repeat: int = 2) -> str:
        """Call `to_number`, speak `text`, return the Twilio call SID."""
        to_number = to_number.replace("whatsapp:", "")
        if not self.is_allowed(to_number):
            raise VoiceError(f"{to_number} is not an allowed number")
        twiml = build_twiml(text, repeat=repeat, voice=self.voice)
        try:
            resp = self._http.post(
                self._url, data={"To": to_number, "From": self.from_number, "Twiml": twiml}
            )
        except httpx.HTTPError as exc:
            raise VoiceError(f"could not reach Twilio: {exc}") from exc
        if resp.status_code >= 400:
            detail: Any = {}
            try:
                detail = resp.json()
            except ValueError:
                pass
            message = detail.get("message") if isinstance(detail, dict) else None
            code = detail.get("code") if isinstance(detail, dict) else None
            hint = ""
            if code == 21219 or (message and "unverified" in message.lower()):
                hint = " On a Twilio trial account, verify your own number under Phone Numbers → Verified Caller IDs."
            raise VoiceError(f"Twilio refused the call ({resp.status_code}): {message or resp.text}.{hint}")
        return str(resp.json().get("sid", ""))

    def close(self) -> None:
        self._http.close()


def build_voice(config: "Config") -> TwilioVoice:
    """A caller for the configured account that may only ring USER_PHONE."""
    if not config.has_voice():
        raise ValueError(
            "phone calls need TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, a voice number "
            "(TWILIO_VOICE_FROM or TWILIO_FROM) and USER_PHONE"
        )
    return TwilioVoice(
        config.twilio_account_sid,
        config.twilio_auth_token,
        config.voice_from,
        {config.user_phone},
    )
