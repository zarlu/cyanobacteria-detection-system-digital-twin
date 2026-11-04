"""Builders for ingest messages used across API tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

T0 = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
STEP = timedelta(minutes=15)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def reading(ts: datetime, channel: str, value: float, unit: str, **extra: Any) -> dict[str, Any]:
    r: dict[str, Any] = {"ts": iso(ts), "channel": channel, "value": value, "unit": unit}
    r.update(extra)
    return r


def message(node_id: str, readings: list[dict[str, Any]], source: str = "sensor") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "node_id": node_id,
        "source": source,
        "sent_at": iso(datetime.now(UTC)),
        "firmware": "test",
        "readings": readings,
    }


def temp_series(n: int, start: datetime = T0, base: float = 20.0) -> list[dict[str, Any]]:
    """n water_temp readings 15 min apart with a small deterministic wobble."""
    return [
        reading(start + i * STEP, "water_temp", round(base + 0.1 * (i % 5), 3), "degC")
        for i in range(n)
    ]
