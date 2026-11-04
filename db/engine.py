"""Engine and session factory. SQLite gets WAL and foreign keys on every connection."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from db.models import Base

log = logging.getLogger(__name__)

DEFAULT_DB_URL = "sqlite:///data/twin.db"


def db_url_from_env() -> str:
    """Return ``DB_URL`` from the environment or the default."""
    return os.environ.get("DB_URL", DEFAULT_DB_URL)


def make_engine(db_url: str | None = None, *, echo: bool = False) -> Engine:
    """Create an engine. For SQLite files the parent directory is created and WAL is enabled."""
    url = make_url(db_url or db_url_from_env())
    connect_args: dict[str, Any] = {}
    if url.get_backend_name() == "sqlite":
        connect_args["check_same_thread"] = False
        if url.database and url.database != ":memory:":
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, echo=echo, connect_args=connect_args, future=True)
    if url.get_backend_name() == "sqlite":

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

    return engine


def init_db(engine: Engine) -> None:
    """Create all tables (idempotent)."""
    Base.metadata.create_all(engine)
    log.info("database ready at %s", engine.url.render_as_string(hide_password=True))


def drop_all(engine: Engine) -> None:
    """Drop every table. Used by ``python -m db.reset``."""
    Base.metadata.drop_all(engine)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a session factory bound to ``engine``."""
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
