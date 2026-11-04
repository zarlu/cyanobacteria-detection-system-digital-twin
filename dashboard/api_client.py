"""The dashboard's only way to get data: HTTP calls to the API (no SQL, no business logic)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

DEFAULT_API_URL = "http://localhost:8000"


class ApiError(Exception):
    """The API could not be reached or answered with an error."""


@dataclass(frozen=True)
class ApiClient:
    """Thin httpx wrapper. Every method raises ``ApiError`` instead of leaking exceptions."""

    base_url: str
    timeout: float = 15.0

    @classmethod
    def from_env(cls) -> ApiClient:
        return cls(base_url=os.environ.get("API_URL", DEFAULT_API_URL).rstrip("/"))

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            r = httpx.get(f"{self.base_url}{path}", params=clean, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise ApiError(f"API not reachable at {self.base_url} ({type(exc).__name__})") from exc
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise ApiError(f"{path} -> HTTP {r.status_code}: {detail}")
        return r.json()

    def health(self) -> dict[str, Any]:
        return self._get("/health")

    def nodes(self) -> list[dict[str, Any]]:
        return self._get("/nodes")

    def channels(self) -> list[dict[str, Any]]:
        return self._get("/channels")

    def reach(self) -> dict[str, Any]:
        return self._get("/reach")

    def observations(
        self,
        *,
        node_id: str | None = None,
        channel: str | None = None,
        kind: str | None = None,
        source: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        min_quality: str | None = None,
        resample: str | None = None,
        agg: str | None = None,
    ) -> dict[str, Any]:
        """Columnar observations exactly as ``GET /observations`` returns them."""
        return self._get(
            "/observations",
            {
                "node_id": node_id,
                "channel": channel,
                "kind": kind,
                "source": source,
                "start": start.isoformat().replace("+00:00", "Z") if start else None,
                "end": end.isoformat().replace("+00:00", "Z") if end else None,
                "min_quality": min_quality,
                "resample": resample,
                "agg": agg,
            },
        )
