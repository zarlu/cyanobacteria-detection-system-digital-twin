"""Shared fixtures: temporary SQLite database, app and TestClient."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from api.app import create_app
from api.settings import Settings
from db.engine import init_db, make_engine, make_session_factory

TEST_API_KEY = "test-key"


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    """A fresh SQLite file URL per test."""
    return f"sqlite:///{tmp_path / 'test.db'}"


@pytest.fixture
def engine(db_url: str) -> Iterator[Engine]:
    """Engine with tables created."""
    eng = make_engine(db_url)
    init_db(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """A session on the temporary database."""
    with make_session_factory(engine)() as s:
        yield s


@pytest.fixture
def settings(db_url: str) -> Settings:
    """Settings for a test app."""
    return Settings(api_key=TEST_API_KEY, db_url=db_url)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """TestClient on an app bound to the temporary database."""
    app = create_app(settings)
    with TestClient(app) as c:
        yield c
    app.state.engine.dispose()


@pytest.fixture
def auth() -> dict[str, str]:
    """Headers with the test API key."""
    return {"X-API-Key": TEST_API_KEY}
