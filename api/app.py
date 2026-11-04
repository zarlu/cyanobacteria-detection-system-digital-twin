"""App factory. ``uvicorn api.app:create_app --factory`` or ``create_app(settings)`` in tests."""

from __future__ import annotations

import logging

from fastapi import FastAPI

from api import __version__
from api.routers import channels, health, ingest, labels, nodes, observations, reach
from api.settings import Settings
from db.engine import init_db, make_engine, make_session_factory
from db.seed import seed_all

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI app, create tables and seed channels and nodes.

    ``settings`` defaults to the environment, so tests can point the app at a temporary database.
    """
    settings = settings or Settings.from_env()
    engine = make_engine(settings.db_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        seed_all(session)

    app = FastAPI(
        title="Cyanobacteria Detection System Digital Twin API",
        version=__version__,
        description=(
            "Ingest and read API for the TAMIU Rio Grande water-quality twin. "
            "UTC everywhere; simulated data is labeled source='simulator'."
        ),
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    for r in (health, ingest, nodes, channels, reach, observations, labels):
        app.include_router(r.router)
    log.info("app created (db=%s)", settings.db_url)
    return app
