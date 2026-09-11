"""Configuration loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MODEL = "claude-opus-5"
VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")


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


@dataclass
class Config:
    model: str = DEFAULT_MODEL
    effort: str = "medium"
    max_tokens: int = 16000
    user_name: str = ""
    data_dir: Path = field(default_factory=lambda: Path.home() / ".my-assistant")
    web_search: bool = False
    fallbacks: bool = True
    timezone: str = ""

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        effort = os.environ.get("ASSISTANT_EFFORT", "medium").strip().lower()
        if effort not in VALID_EFFORTS:
            raise ValueError(
                f"ASSISTANT_EFFORT must be one of {', '.join(VALID_EFFORTS)}; got {effort!r}"
            )
        data_dir = Path(os.environ.get("ASSISTANT_DATA_DIR", "~/.my-assistant")).expanduser()
        return cls(
            model=os.environ.get("ASSISTANT_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
            effort=effort,
            max_tokens=int(os.environ.get("ASSISTANT_MAX_TOKENS", "16000")),
            user_name=os.environ.get("ASSISTANT_USER_NAME", "").strip(),
            data_dir=data_dir,
            web_search=_truthy(os.environ.get("ASSISTANT_WEB_SEARCH")),
            fallbacks=_truthy(os.environ.get("ASSISTANT_FALLBACKS", "1")),
            timezone=os.environ.get("ASSISTANT_TIMEZONE", "").strip(),
        )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "assistant.db"
