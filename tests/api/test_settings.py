"""Settings: the API refuses to start without an API key (no key literal lives in code)."""

from __future__ import annotations

import pytest

from api.settings import Settings


def test_from_env_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="API_KEY is not set"):
        Settings.from_env()
    monkeypatch.setenv("API_KEY", "   ")
    with pytest.raises(RuntimeError):
        Settings.from_env()
    monkeypatch.setenv("API_KEY", "abc")
    monkeypatch.setenv("DB_URL", "sqlite:///x.db")
    s = Settings.from_env()
    assert s.api_key == "abc" and s.db_url == "sqlite:///x.db"


def test_dev_key_literal_only_in_env_example() -> None:
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    offenders = []
    for p in repo.rglob("*.py"):
        if ".venv" in p.parts:
            continue
        if "dev-key-change-me" in p.read_text(encoding="utf-8") and p.name != "test_settings.py":
            offenders.append(str(p.relative_to(repo)))
    assert offenders == [], offenders
