"""Shared FastAPI dependencies: settings, database session, API key check."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from api.settings import Settings


def get_settings(request: Request) -> Settings:
    """The settings the app was created with."""
    return request.app.state.settings


def get_session(request: Request) -> Iterator[Session]:
    """One SQLAlchemy session per request."""
    session: Session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


def require_api_key(
    settings: Annotated[Settings, Depends(get_settings)],
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    """Reject the request with 401 unless ``X-API-Key`` matches ``API_KEY``."""
    if x_api_key is None or x_api_key != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid X-API-Key header",
        )


SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[Session, Depends(get_session)]
