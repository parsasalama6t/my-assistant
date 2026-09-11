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
