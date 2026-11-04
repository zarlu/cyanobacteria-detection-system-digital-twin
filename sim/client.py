"""HTTP client of the simulator: posts to ``/ingest`` and ``/labels`` with retries and backoff."""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

TRANSIENT_STATUS = {429, 500, 502, 503, 504}


@dataclass
class PostStats:
    """Totals reported at the end of a run."""

    requests: int = 0
    accepted: int = 0
    duplicates: int = 0
    rejected: int = 0
    failed_requests: int = 0
    rejection_reasons: Counter = field(default_factory=Counter)


class SimClient:
    """Posts contract messages like a node would. ``http`` is injectable (tests pass TestClient)."""

    def __init__(
        self,
        api_url: str,
        api_key: str,
        http: httpx.Client | None = None,
        *,
        max_tries: int = 6,
        base_backoff_s: float = 0.5,
        sleep=time.sleep,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.headers = {"X-API-Key": api_key}
        self.http = http or httpx.Client(timeout=60.0)
        self.max_tries = max_tries
        self.base_backoff_s = base_backoff_s
        self.sleep = sleep
        self.stats = PostStats()

    def post(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any] | None:
        """POST with retries on transport errors and 5xx/429; JSON on success, None on failure."""
        url = f"{self.api_url}{endpoint}"
        for attempt in range(1, self.max_tries + 1):
            try:
                resp = self.http.post(url, json=body, headers=self.headers)
            except httpx.HTTPError as exc:
                self._backoff(attempt, f"{type(exc).__name__}: {exc}")
                continue
            if resp.status_code in TRANSIENT_STATUS:
                self._backoff(attempt, f"HTTP {resp.status_code}")
                continue
            self.stats.requests += 1
            if resp.status_code >= 400:
                self.stats.failed_requests += 1
                log.error("%s -> HTTP %s: %s", endpoint, resp.status_code, resp.text[:300])
                return None
            data = resp.json()
            self.stats.accepted += int(data.get("accepted", 0))
            self.stats.duplicates += int(data.get("duplicates", 0))
            rejected = data.get("rejected", [])
            self.stats.rejected += len(rejected)
            for r in rejected[:5]:
                self.stats.rejection_reasons[r.get("reason", "?")[:80]] += 1
            if rejected:
                log.warning(
                    "%s: %d readings rejected, e.g. %s", endpoint, len(rejected), rejected[0]
                )
            return data
        self.stats.failed_requests += 1
        log.error("%s: giving up after %d attempts", endpoint, self.max_tries)
        return None

    def _backoff(self, attempt: int, why: str) -> None:
        if attempt >= self.max_tries:
            return
        delay = self.base_backoff_s * (2 ** (attempt - 1))
        log.warning(
            "transient error (%s); retry %d/%d in %.1fs", why, attempt, self.max_tries - 1, delay
        )
        self.sleep(delay)


class DryRunWriter:
    """Writes each would-be request as one JSONL line instead of posting it."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", encoding="utf-8")
        self.stats = PostStats()

    def post(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any] | None:
        self._fh.write(
            json.dumps({"endpoint": endpoint, "body": body}, sort_keys=True, separators=(",", ":"))
            + "\n"
        )
        self.stats.requests += 1
        n = len(body.get("readings", [])) if endpoint == "/ingest" else 1
        self.stats.accepted += n
        return {"accepted": n, "duplicates": 0, "rejected": []}

    def close(self) -> None:
        self._fh.close()
