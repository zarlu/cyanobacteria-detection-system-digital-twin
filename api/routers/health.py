"""``GET /health``: version, database check, UTC now."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from api import __version__
from api.deps import SessionDep

router = APIRouter(tags=["health"])


class Health(BaseModel):
    """Health response."""

    version: str
    db_ok: bool
    utc_now: datetime
    schema_version: int


@router.get("/health", response_model=Health)
def health(session: SessionDep) -> Health:
    """Report service version, whether the database answers, and the server's UTC time."""
    try:
        session.execute(text("SELECT 1"))
        db_ok = True
    except Exception:  # noqa: BLE001 - any DB failure means "not ok"
        db_ok = False
    return Health(version=__version__, db_ok=db_ok, utc_now=datetime.now(UTC), schema_version=1)
