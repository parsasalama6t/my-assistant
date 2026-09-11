from __future__ import annotations

import os

import pytest

from assistant import cli


def _run(monkeypatch, tmp_path, env_text: str, capsys, token_result=(None, "offline")):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(env_text)
    for key in ("ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TWILIO_ACCOUNT_SID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ASSISTANT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(cli, "_validate_telegram_token", lambda token: token_result)
    with pytest.raises(SystemExit) as exc:
        cli.main(["doctor"])
    return exc.value.code, capsys.readouterr().out


def test_doctor_flags_placeholder_key_and_missing_token(monkeypatch, tmp_path, capsys) -> None:
    code, out = _run(monkeypatch, tmp_path, "ANTHROPIC_API_KEY=sk-ant-...\n", capsys)
    assert code == 1
    assert "[FIX] ANTHROPIC_API_KEY is missing or still the placeholder" in out
    assert "[FIX] TELEGRAM_BOT_TOKEN is not set" in out
    assert "[FIX] daemon is not running" in out
    assert "Start with the first one" in out


def test_doctor_passes_when_configured_and_daemon_running(monkeypatch, tmp_path, capsys) -> None:
    env = "ANTHROPIC_API_KEY=sk-ant-abc\nTELEGRAM_BOT_TOKEN=1:x\nTELEGRAM_CHAT_ID=42\n"
    data = tmp_path / "data"
    data.mkdir()
    (data / "daemon.pid").write_text(str(os.getpid()))
    code, out = _run(monkeypatch, tmp_path, env, capsys, token_result=(True, "mybot"))
    assert code == 0
    assert "bot is @mybot" in out
    assert "daemon is running" in out
    assert "Everything needed is in place" in out


def test_doctor_reports_missing_chat_id(monkeypatch, tmp_path, capsys) -> None:
    env = "ANTHROPIC_API_KEY=sk-ant-abc\nTELEGRAM_BOT_TOKEN=1:x\n"
    code, out = _run(monkeypatch, tmp_path, env, capsys, token_result=(True, "mybot"))
    assert code == 1
    assert "TELEGRAM_CHAT_ID is not set" in out and "channels whoami" in out
