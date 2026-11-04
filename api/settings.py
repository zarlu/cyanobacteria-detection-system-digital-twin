"""Runtime settings read from the environment (``.env`` is loaded by ``uv run --env-file``)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from db.engine import DEFAULT_DB_URL


@dataclass(frozen=True)
class Settings:
    """Settings for one app instance."""

    api_key: str
    db_url: str

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from ``API_KEY`` (required) and ``DB_URL``."""
        api_key = os.environ.get("API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "API_KEY is not set. Copy .env.example to .env (make setup does this) or export "
                "API_KEY before starting the API."
            )
        return cls(api_key=api_key, db_url=os.environ.get("DB_URL", DEFAULT_DB_URL))
