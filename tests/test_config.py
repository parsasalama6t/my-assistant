from __future__ import annotations

import pytest

from assistant.config import Config, load_dotenv


def test_from_env_defaults(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    for key in ("ASSISTANT_MODEL", "ASSISTANT_EFFORT", "ASSISTANT_FALLBACKS", "ASSISTANT_WEB_SEARCH"):
        monkeypatch.delenv(key, raising=False)
    cfg = Config.from_env()
    assert cfg.model == "claude-opus-5" and cfg.effort == "medium"
    assert cfg.fallbacks is True and cfg.web_search is False
    assert cfg.db_path.name == "assistant.db"


def test_dotenv_and_overrides(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        'ASSISTANT_EFFORT="high"\nASSISTANT_WEB_SEARCH=1\nASSISTANT_USER_NAME=Parsa\n# comment\n'
    )
    for key in ("ASSISTANT_EFFORT", "ASSISTANT_WEB_SEARCH", "ASSISTANT_USER_NAME"):
        monkeypatch.delenv(key, raising=False)
    load_dotenv(tmp_path / ".env")
    cfg = Config.from_env()
    assert cfg.effort == "high" and cfg.web_search is True and cfg.user_name == "Parsa"


def test_invalid_effort(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ASSISTANT_EFFORT", "turbo")
    with pytest.raises(ValueError):
        Config.from_env()


SCHED_KEYS = (
    "ASSISTANT_TIMEZONE", "ASSISTANT_DEFAULT_CHANNEL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM", "TWILIO_WHATSAPP_FROM",
    "TWILIO_WEBHOOK_URL", "TWILIO_WEBHOOK_PORT", "USER_PHONE", "ASSISTANT_MORNING_BRIEFING",
    "ASSISTANT_EVENING_REVIEW", "ASSISTANT_EVENT_LEAD_MINUTES", "ASSISTANT_TASK_LEAD_MINUTES",
    "ASSISTANT_CATCHUP_GRACE_MINUTES", "ASSISTANT_TICK_SECONDS", "ASSISTANT_SESSION_IDLE_HOURS",
)


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for key in SCHED_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_scheduling_defaults(clean_env) -> None:
    cfg = Config.from_env()
    assert cfg.timezone == "America/Toronto" and cfg.tz.key == "America/Toronto"
    assert cfg.default_channel is None and cfg.has_messaging() is False and cfg.default_target() is None
    assert cfg.morning_briefing == "07:30" and cfg.evening_review == "21:00"
    assert cfg.event_lead_minutes == 15 and cfg.task_lead_minutes == 0
    assert cfg.catchup_grace_minutes == 120 and cfg.tick_seconds == 30 and cfg.session_idle_hours == 12
    assert cfg.twilio_whatsapp_from == "whatsapp:+14155238886" and cfg.twilio_webhook_port == 8081
    assert cfg.telegram_chat_ids == []


def test_scheduling_env_overrides(clean_env) -> None:
    clean_env.setenv("ASSISTANT_TIMEZONE", "Europe/Berlin")
    clean_env.setenv("TELEGRAM_BOT_TOKEN", "tok")
    clean_env.setenv("TELEGRAM_CHAT_ID", " 11, 22 ,,")
    clean_env.setenv("ASSISTANT_MORNING_BRIEFING", "06:45")
    clean_env.setenv("ASSISTANT_EVENING_REVIEW", "")
    clean_env.setenv("ASSISTANT_EVENT_LEAD_MINUTES", "30")
    clean_env.setenv("ASSISTANT_TICK_SECONDS", "5")
    clean_env.setenv("TWILIO_WEBHOOK_PORT", "9000")
    cfg = Config.from_env()
    assert cfg.timezone == "Europe/Berlin"
    assert cfg.default_channel == "telegram" and cfg.telegram_chat_ids == ["11", "22"]
    assert cfg.default_target() == ("telegram", "11") and cfg.has_messaging()
    assert cfg.morning_briefing == "06:45" and cfg.evening_review == ""
    assert cfg.event_lead_minutes == 30 and cfg.tick_seconds == 5 and cfg.twilio_webhook_port == 9000


def test_invalid_timezone_and_times_raise(clean_env) -> None:
    clean_env.setenv("ASSISTANT_TIMEZONE", "Mars/Olympus")
    with pytest.raises(ValueError, match="ASSISTANT_TIMEZONE"):
        Config.from_env()
    clean_env.delenv("ASSISTANT_TIMEZONE")
    clean_env.setenv("ASSISTANT_MORNING_BRIEFING", "7:30am")
    with pytest.raises(ValueError, match="ASSISTANT_MORNING_BRIEFING"):
        Config.from_env()
    clean_env.delenv("ASSISTANT_MORNING_BRIEFING")
    clean_env.setenv("ASSISTANT_EVENING_REVIEW", "25:00")
    with pytest.raises(ValueError, match="ASSISTANT_EVENING_REVIEW"):
        Config.from_env()
    clean_env.delenv("ASSISTANT_EVENING_REVIEW")
    clean_env.setenv("ASSISTANT_DEFAULT_CHANNEL", "pigeon")
    with pytest.raises(ValueError, match="ASSISTANT_DEFAULT_CHANNEL"):
        Config.from_env()
    clean_env.delenv("ASSISTANT_DEFAULT_CHANNEL")
    clean_env.setenv("ASSISTANT_TICK_SECONDS", "fast")
    with pytest.raises(ValueError, match="ASSISTANT_TICK_SECONDS"):
        Config.from_env()


def test_default_channel_precedence_and_targets(clean_env) -> None:
    # Twilio only -> twilio; WhatsApp sandbox sender is default, so the target is whatsapp:.
    clean_env.setenv("TWILIO_ACCOUNT_SID", "AC1")
    clean_env.setenv("USER_PHONE", "+14165550100")
    cfg = Config.from_env()
    assert cfg.default_channel == "twilio"
    assert cfg.default_target() == ("twilio", "whatsapp:+14165550100")
    assert set(cfg.allowed_chat_ids("twilio")) == {"+14165550100", "whatsapp:+14165550100"}

    # An SMS sender switches the target to plain SMS.
    clean_env.setenv("TWILIO_FROM", "+15550001111")
    assert Config.from_env().default_target() == ("twilio", "+14165550100")

    # Telegram wins over Twilio when both are configured...
    clean_env.setenv("TELEGRAM_BOT_TOKEN", "tok")
    clean_env.setenv("TELEGRAM_CHAT_ID", "42")
    cfg = Config.from_env()
    assert cfg.default_channel == "telegram" and cfg.default_target() == ("telegram", "42")

    # ...unless ASSISTANT_DEFAULT_CHANNEL says otherwise.
    clean_env.setenv("ASSISTANT_DEFAULT_CHANNEL", "twilio")
    assert Config.from_env().default_target() == ("twilio", "+14165550100")

    # Twilio without USER_PHONE has nowhere to send.
    clean_env.delenv("USER_PHONE")
    cfg = Config.from_env()
    assert cfg.default_target() is None and cfg.has_messaging() is False

    # Telegram without a chat id likewise.
    clean_env.setenv("ASSISTANT_DEFAULT_CHANNEL", "telegram")
    clean_env.delenv("TELEGRAM_CHAT_ID")
    assert Config.from_env().default_target() is None
