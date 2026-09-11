from __future__ import annotations

from pathlib import Path

import pytest

from assistant import cli
from assistant.store import Store


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)  # no stray .env is picked up
    monkeypatch.setenv("ASSISTANT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    monkeypatch.setenv("ASSISTANT_TIMEZONE", "America/Toronto")
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("ASSISTANT_DEFAULT_CHANNEL", raising=False)
    return tmp_path


def run(argv: list[str], capsys) -> tuple[int, str, str]:
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    out, err = capsys.readouterr()
    return int(exc.value.code or 0), out, err


def test_schedule_add_list_cancel(env: Path, capsys) -> None:
    code, out, err = run(["schedule", "add", "Call mom", "--at", "2099-09-12T15:00", "--repeat", "weekdays"], capsys)
    assert code == 0, err
    assert "Call mom" in out and "weekdays" in out and "telegram" in out

    code, out, _ = run(["schedule", "list"], capsys)
    assert code == 0
    assert "Call mom" in out and "2099-09-14 15:00" in out  # Sept 12 2099 is a Saturday -> Monday

    store = Store(env / "assistant.db")
    schedule_id = store.list_schedules()[0]["id"]
    store.close()

    code, out, _ = run(["schedule", "cancel", str(schedule_id)], capsys)
    assert code == 0 and f"Cancelled #{schedule_id}" in out
    code, out, _ = run(["schedule", "list"], capsys)
    assert "(none)" in out
    code, out, _ = run(["schedule", "list", "--all"], capsys)
    assert "[cancelled]" in out

    code, out, err = run(["schedule", "add", "Past", "--at", "2000-01-01T09:00"], capsys)
    assert code == 1 and "in the past" in err
    code, out, err = run(["schedule", "cancel", "9999"], capsys)
    assert code == 1


def test_schedule_sync_and_deliveries(env: Path, capsys) -> None:
    code, out, _ = run(["schedule", "sync"], capsys)
    assert code == 0 and "2 fixed" in out
    code, out, _ = run(["schedule", "list", "--all"], capsys)
    assert "system:morning" in out and "system:evening" in out
    code, out, _ = run(["schedule", "deliveries"], capsys)
    assert code == 0 and "(none)" in out


def test_channels_status(env: Path, capsys) -> None:
    code, out, _ = run(["channels", "status"], capsys)
    assert code == 0
    assert "telegram: configured, chat ids 111" in out
    assert "twilio: not configured" in out
    assert "default target: telegram 111" in out
    assert "timezone: America/Toronto" in out
    assert "morning briefing: 07:30" in out and "evening review: 21:00" in out
    assert "voice: off" in out and "USER_PHONE" in out


def test_channels_status_and_test_call_with_voice(env: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    code, out, err = run(["channels", "test", "--call"], capsys)
    assert code == 2 and "not configured" in err

    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC1")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.setenv("TWILIO_VOICE_FROM", "+15550001111")
    monkeypatch.setenv("USER_PHONE", "+14165550100")
    code, out, _ = run(["channels", "status"], capsys)
    assert code == 0 and "voice: on (from +15550001111, calls +14165550100)" in out

    placed: list[tuple[str, str]] = []

    class StubVoice:
        def call(self, to_number: str, text: str, repeat: int = 2) -> str:
            placed.append((to_number, text))
            return "CA77"

        def close(self) -> None:
            pass

    import assistant.voice as voice_module

    monkeypatch.setattr(voice_module, "build_voice", lambda config: StubVoice())
    code, out, err = run(["channels", "test", "--call", "--text", "Testing one two"], capsys)
    assert code == 0, err
    assert placed == [("+14165550100", "Testing one two")] and "CA77" in out


def test_schedule_add_with_priority(env: Path, capsys) -> None:
    code, out, err = run(["schedule", "add", "Take meds", "--at", "2099-09-12T15:00", "--priority", "important"], capsys)
    assert code == 0, err
    assert "(important)" in out and "text only" in out  # voice is off in this env
    store = Store(env / "assistant.db")
    row = store.list_schedules()[0]
    store.close()
    assert row["priority"] == "important"
    code, out, _ = run(["schedule", "list"], capsys)
    assert "!important" in out
    with pytest.raises(SystemExit):
        cli.main(["schedule", "add", "x", "--at", "2099-09-12T15:00", "--priority", "loud"])


def test_daemon_refuses_without_channels(env: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    monkeypatch.delenv("TELEGRAM_CHAT_ID")
    code, out, err = run(["daemon"], capsys)
    assert code == 2
    assert "No messaging channel is configured" in err and ".env.example" in err


def test_daemon_once_prints_tick_counts(env: Path, capsys) -> None:
    code, out, err = run(["daemon", "--once"], capsys)
    assert code == 0, err
    assert out.strip().startswith("sent 0, skipped 0, failed 0")


def test_write_env_merges_without_clobbering(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("# comment\nANTHROPIC_API_KEY=old\nTELEGRAM_BOT_TOKEN=\n", encoding="utf-8")
    written = cli.write_env(
        path,
        {"ANTHROPIC_API_KEY": "new", "TELEGRAM_BOT_TOKEN": "t", "ASSISTANT_TIMEZONE": "Europe/Paris"},
        confirm=lambda key: False,
    )
    assert written == ["TELEGRAM_BOT_TOKEN", "ASSISTANT_TIMEZONE"]
    content = path.read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY=old" in content and "# comment" in content
    assert "TELEGRAM_BOT_TOKEN=t" in content and "ASSISTANT_TIMEZONE=Europe/Paris" in content

    cli.write_env(path, {"ANTHROPIC_API_KEY": "new"}, confirm=lambda key: True)
    assert "ANTHROPIC_API_KEY=new" in path.read_text(encoding="utf-8")
