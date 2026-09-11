"""Configuration loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_TIMEZONE = "America/Toronto"
VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")
MESSAGING_CHANNELS = ("telegram", "twilio")

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def load_dotenv(path: Path | None = None) -> None:
    """Populate os.environ from a .env file without overriding existing values.

    Intentionally minimal so the project does not need python-dotenv.
    """
    path = path or Path.cwd() / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int_env(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer; got {raw!r}") from exc


def validate_hhmm(value: str, name: str = "time") -> str:
    """Return a normalized 'HH:MM' string or raise ValueError."""
    value = value.strip()
    if not _HHMM.match(value):
        raise ValueError(f"{name} must be HH:MM (24-hour); got {value!r}")
    return value


@dataclass
class Config:
    model: str = DEFAULT_MODEL
    effort: str = "medium"
    max_tokens: int = 16000
    user_name: str = ""
    data_dir: Path = field(default_factory=lambda: Path.home() / ".my-assistant")
    web_search: bool = False
    fallbacks: bool = True
    timezone: str = DEFAULT_TIMEZONE
    google: bool = True  # use Google Calendar/Gmail when a login token exists
    google_credentials: Path | None = None  # OAuth client file; defaults to <data_dir>/google_credentials.json

    # Messaging channels. `default_channel` is where scheduled texts go when the
    # request did not come from a chat; None means messaging is off.
    default_channel: str | None = None
    telegram_bot_token: str = ""
    telegram_chat_ids: list[str] = field(default_factory=list)
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from: str = ""  # SMS sender number; leave empty to use WhatsApp only
    twilio_voice_from: str = ""  # voice-capable number for calls; defaults to twilio_from
    twilio_whatsapp_from: str = "whatsapp:+14155238886"
    twilio_webhook_url: str = ""
    twilio_webhook_port: int = 8081
    user_phone: str = ""  # E.164, e.g. +14165550100

    # Scheduling.
    morning_briefing: str = "07:30"  # local HH:MM; "" disables
    evening_review: str = "21:00"
    event_lead_minutes: int = 15
    task_lead_minutes: int = 0
    catchup_grace_minutes: int = 120
    tick_seconds: int = 30
    session_idle_hours: int = 12
    escalate_minutes: int = 10  # 'important' texts turn into a call after this long without a reply

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        effort = _env("ASSISTANT_EFFORT", "medium").lower()
        if effort not in VALID_EFFORTS:
            raise ValueError(
                f"ASSISTANT_EFFORT must be one of {', '.join(VALID_EFFORTS)}; got {effort!r}"
            )
        data_dir = Path(os.environ.get("ASSISTANT_DATA_DIR", "~/.my-assistant")).expanduser()

        tz_name = _env("ASSISTANT_TIMEZONE") or DEFAULT_TIMEZONE
        try:
            ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"ASSISTANT_TIMEZONE is not a known IANA zone: {tz_name!r}") from exc

        telegram_token = _env("TELEGRAM_BOT_TOKEN")
        telegram_chat_ids = [c.strip() for c in _env("TELEGRAM_CHAT_ID").split(",") if c.strip()]
        twilio_sid = _env("TWILIO_ACCOUNT_SID")

        default_channel = _env("ASSISTANT_DEFAULT_CHANNEL").lower() or None
        if default_channel is None:
            if telegram_token:
                default_channel = "telegram"
            elif twilio_sid:
                default_channel = "twilio"
        elif default_channel not in MESSAGING_CHANNELS:
            raise ValueError(
                f"ASSISTANT_DEFAULT_CHANNEL must be one of {', '.join(MESSAGING_CHANNELS)};"
                f" got {default_channel!r}"
            )

        morning = _env("ASSISTANT_MORNING_BRIEFING", "07:30")
        evening = _env("ASSISTANT_EVENING_REVIEW", "21:00")
        if morning:
            morning = validate_hhmm(morning, "ASSISTANT_MORNING_BRIEFING")
        if evening:
            evening = validate_hhmm(evening, "ASSISTANT_EVENING_REVIEW")

        return cls(
            model=_env("ASSISTANT_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL,
            effort=effort,
            max_tokens=_int_env("ASSISTANT_MAX_TOKENS", 16000),
            user_name=_env("ASSISTANT_USER_NAME"),
            data_dir=data_dir,
            web_search=_truthy(os.environ.get("ASSISTANT_WEB_SEARCH")),
            fallbacks=_truthy(os.environ.get("ASSISTANT_FALLBACKS", "1")),
            timezone=tz_name,
            google=_truthy(os.environ.get("ASSISTANT_GOOGLE", "1")),
            google_credentials=(
                Path(os.environ["ASSISTANT_GOOGLE_CREDENTIALS"]).expanduser()
                if os.environ.get("ASSISTANT_GOOGLE_CREDENTIALS")
                else None
            ),
            default_channel=default_channel,
            telegram_bot_token=telegram_token,
            telegram_chat_ids=telegram_chat_ids,
            twilio_account_sid=twilio_sid,
            twilio_auth_token=_env("TWILIO_AUTH_TOKEN"),
            twilio_from=_env("TWILIO_FROM"),
            twilio_voice_from=_env("TWILIO_VOICE_FROM") or _env("TWILIO_FROM"),
            twilio_whatsapp_from=os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886").strip(),
            twilio_webhook_url=_env("TWILIO_WEBHOOK_URL"),
            twilio_webhook_port=_int_env("TWILIO_WEBHOOK_PORT", 8081),
            user_phone=_env("USER_PHONE"),
            morning_briefing=morning,
            evening_review=evening,
            event_lead_minutes=_int_env("ASSISTANT_EVENT_LEAD_MINUTES", 15),
            task_lead_minutes=_int_env("ASSISTANT_TASK_LEAD_MINUTES", 0),
            catchup_grace_minutes=_int_env("ASSISTANT_CATCHUP_GRACE_MINUTES", 120),
            tick_seconds=_int_env("ASSISTANT_TICK_SECONDS", 30),
            session_idle_hours=_int_env("ASSISTANT_SESSION_IDLE_HOURS", 12),
            escalate_minutes=_int_env("ASSISTANT_ESCALATE_MINUTES", 10),
        )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "assistant.db"

    @property
    def google_credentials_path(self) -> Path:
        return self.google_credentials or (self.data_dir / "google_credentials.json")

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone or DEFAULT_TIMEZONE)

    # ------------------------------------------------------------ messaging
    def has_messaging(self) -> bool:
        """True when at least one outbound channel can actually reach the user."""
        return self.default_target() is not None

    def default_target(self) -> tuple[str, str] | None:
        """Where unsolicited texts go, as (channel, chat_id), or None.

        telegram -> the first allowlisted chat id.
        twilio   -> "whatsapp:<USER_PHONE>" when only the WhatsApp sender is set,
                    otherwise USER_PHONE as an SMS destination.
        """
        if self.default_channel == "telegram":
            if self.telegram_bot_token and self.telegram_chat_ids:
                return ("telegram", self.telegram_chat_ids[0])
            return None
        if self.default_channel == "twilio":
            if not (self.twilio_account_sid and self.user_phone):
                return None
            if self.twilio_whatsapp_from and not self.twilio_from:
                return ("twilio", f"whatsapp:{self.user_phone}")
            return ("twilio", self.user_phone)
        return None

    # ---------------------------------------------------------------- voice
    @property
    def voice_from(self) -> str:
        """Number calls are placed from: TWILIO_VOICE_FROM, else the SMS sender."""
        return self.twilio_voice_from or self.twilio_from

    def has_voice(self) -> bool:
        """True when the assistant can place phone calls to the user."""
        return bool(
            self.twilio_account_sid and self.twilio_auth_token and self.voice_from and self.user_phone
        )

    def allowed_chat_ids(self, channel: str) -> list[str]:
        """Chat ids the assistant may message on `channel` (empty = no allowlist known)."""
        if channel == "telegram":
            return list(self.telegram_chat_ids)
        if channel == "twilio" and self.user_phone:
            return [self.user_phone, f"whatsapp:{self.user_phone}"]
        return []
