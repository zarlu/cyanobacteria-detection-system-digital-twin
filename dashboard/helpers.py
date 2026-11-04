"""Pure helpers for the dashboard (no Streamlit): time zones, quality encoding, parameters.

Everything here is unit-tested; the views only wire these functions to widgets.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

DEFAULT_TZ = "America/Chicago"
TZ_OPTIONS = (DEFAULT_TZ, "UTC")
BANNER_TEXT = "SIMULATED DATA - pipeline validation only, not river performance"

TIME_RANGES: dict[str, timedelta | None] = {
    "last 6 h": timedelta(hours=6),
    "last 24 h": timedelta(hours=24),
    "last 7 d": timedelta(days=7),
    "last 30 d": timedelta(days=30),
    "custom": None,
}

RESAMPLE_OPTIONS: dict[str, str | None] = {
    "raw": None,
    "15 min": "15min",
    "1 h": "1h",
    "1 day": "1D",
}

QUALITY_STYLE: dict[str, dict[str, Any]] = {
    "good": {"mode": "lines", "line": {"width": 1.6}},
    "suspect": {
        "mode": "markers",
        "marker": {"symbol": "circle-open", "color": "orange", "size": 8},
    },
    "bad": {"mode": "markers", "marker": {"symbol": "x", "color": "red", "size": 8}},
}

KEY_CHANNELS = ("water_temp", "ph", "tds", "turbidity", "phosphate_po4")


def time_range_bounds(
    choice: str, now: datetime | None = None, custom: tuple[datetime, datetime] | None = None
) -> tuple[datetime, datetime]:
    """UTC (start, end) for a picker choice. ``custom`` is used when ``choice == 'custom'``."""
    now = now or datetime.now(UTC)
    if choice == "custom":
        if custom is None:
            raise ValueError("custom range needs explicit bounds")
        start, end = custom
        return _utc(start), _utc(end)
    delta = TIME_RANGES[choice]
    if delta is None:
        raise ValueError(f"unknown time range {choice!r}")
    return now - delta, now


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_display_tz(ts: pd.Series | list[Any], tz: str = DEFAULT_TZ) -> pd.Series:
    """Convert UTC timestamps to the display zone (data stays UTC; this is for display only)."""
    s = pd.to_datetime(pd.Series(ts), utc=True)
    return s.dt.tz_convert(ZoneInfo(tz))


def resample_param(label: str) -> str | None:
    """Map a dropdown label to the API ``resample`` value (None = raw)."""
    if label not in RESAMPLE_OPTIONS:
        raise KeyError(f"unknown resample label {label!r}")
    return RESAMPLE_OPTIONS[label]


def has_simulator_rows(sources: list[str] | pd.Series) -> bool:
    """True when any displayed row carries ``source='simulator'`` (drives the banner)."""
    return any(s == "simulator" for s in sources)


def observations_to_frame(body: dict[str, Any]) -> pd.DataFrame:
    """Columnar API response -> tidy DataFrame with a UTC ``ts`` column."""
    cols = {k: v for k, v in body.items() if isinstance(v, list)}
    df = pd.DataFrame(cols)
    if df.empty:
        return pd.DataFrame(
            columns=["ts", "node_id", "channel", "value", "unit", "quality", "kind", "source"]
        )
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df


def split_by_quality(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Rows per quality flag. ``good`` is what the line is drawn through; the others are markers."""
    return {q: df[df["quality"] == q] for q in ("good", "suspect", "bad")}


def minutes_silent_text(minutes: float | None) -> str:
    """Human text for ``/nodes.minutes_since_seen``."""
    if minutes is None:
        return "never seen"
    if minutes < 60:
        return f"{minutes:.0f} min ago"
    if minutes < 48 * 60:
        return f"{minutes / 60:.1f} h ago"
    return f"{minutes / 1440:.1f} d ago"


def latest_per_channel(df: pd.DataFrame) -> dict[str, tuple[float, str, str, datetime]]:
    """Newest (value, unit, quality, ts) per channel from a raw observations frame."""
    out: dict[str, tuple[float, str, str, datetime]] = {}
    if df.empty:
        return out
    for channel, g in df.sort_values("ts").groupby("channel"):
        row = g.iloc[-1]
        out[str(channel)] = (float(row["value"]), str(row["unit"]), str(row["quality"]), row["ts"])
    return out
