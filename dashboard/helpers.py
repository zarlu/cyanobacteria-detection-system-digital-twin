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


# ----------------------------------------------------------------- benchmarks and outlook
REAL_DATA_TEXT = (
    "Real data: IBWC Clean Rivers Program grab samples (source=CRP), about one sample per "
    "station per month. Station names and locations are PLACEHOLDER."
)
OUTLOOK_NOTE = (
    "Seasonal outlook, not a sensor-driven forecast: the share of past samples at this station "
    "near each calendar month that reached the event level. A new sensor reading does not move "
    "it; a new lab label does."
)
ALGO_COLOR = {
    "logistic": "#2a78d6",
    "ridge": "#2a78d6",
    "random_forest": "#eb6834",
    "lightgbm": "#1baf7a",
    "xgboost": "#eda100",
}
BASELINE_COLOR = "#a9a8a2"
LIKELY_COLOR = "#d6453d"
QUIET_COLOR = "#c9c8c2"
METRIC_TITLE = {"pr_auc": "PR-AUC (0 to 1; higher is better)", "mae": "MAE (lower is better)"}
BLOCK_TITLE = {"walk_forward": "walk-forward", "holdout": "final hold-out"}
VERDICT_TEXT = {
    "beats": "beats",
    "worse": "is worse than",
    "no_difference": "no demonstrable difference from",
}


def benchmark_frame(body: dict[str, Any], kind: str, block: str) -> pd.DataFrame:
    """One row per predictor of a benchmark block, baselines first (report order kept)."""
    cols = ["key", "predictor", "is_baseline", "algorithm", "value", "lo", "hi", "color"]
    rows = body.get("kinds", {}).get(kind, {}).get("blocks", {}).get(block, {}).get("rows", [])
    if not rows:
        return pd.DataFrame(columns=cols)
    out = []
    for r in rows:
        if r["value"] is None:
            continue
        entry = {
            "key": r["key"],
            "predictor": r["label"],
            "is_baseline": r["is_baseline"],
            "algorithm": r["algorithm"],
            "value": r["value"],
            "lo": r["lo"],
            "hi": r["hi"],
            "color": BASELINE_COLOR
            if r["is_baseline"]
            else ALGO_COLOR.get(r["algorithm"], "#0b0b0b"),
        }
        for baseline, v in r.get("verdicts", {}).items():
            entry[f"vs {baseline.replace('_', ' ')}"] = VERDICT_TEXT.get(v["verdict"], v["verdict"])
        out.append(entry)
    if not out:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(out)
    return df.sort_values("is_baseline", ascending=False, kind="stable").reset_index(drop=True)


def window_text(outlook: dict[str, Any]) -> tuple[str, str]:
    """(headline, detail) for the next likely bloom window of an outlook response."""
    share = f"{100 * float(outlook.get('likely_probability', 0.5)):.0f}%"
    if not outlook.get("months"):
        return "no outlook", "No labels are stored for this station."
    window = outlook.get("next_window")
    if window is None:
        return (
            "none in the next 12 months",
            f"No calendar month reaches {share} in this station's sample history.",
        )
    start = pd.Timestamp(window["month_start"])
    last = pd.Timestamp(window["last_month_start"])
    span = f"{start:%B %Y}" if window["n_months"] == 1 else f"{start:%B %Y} to {last:%B %Y}"
    days = int(window["days_until"])
    if days <= 0:
        head = "in a likely bloom month now"
    elif days < 14:
        head = f"in about {days} days"
    elif days < 70:
        head = f"in about {round(days / 7)} weeks"
    else:
        head = f"in about {round(days / 30.44)} months"
    detail = (
        f"Likely window: {span}. Up to {100 * window['peak_probability']:.0f}% of past samples "
        f"near those months reached the event level (a month counts as likely at {share})."
    )
    return head, detail


def outlook_table(outlooks: dict[str, dict[str, Any]]) -> pd.DataFrame:
    """One row per node: its next likely bloom window, soonest first, nodes without one last."""
    cols = ["node", "next likely window", "starts", "days until", "months", "peak share",
            "labels at station", "latest label"]  # fmt: skip
    rows = []
    for node, o in outlooks.items():
        if not o.get("months"):
            continue
        w = o.get("next_window")
        rows.append(
            {
                "node": node,
                "next likely window": window_text(o)[0],
                "starts": w["month_start"] if w else None,
                "days until": int(w["days_until"]) if w else None,
                "months": int(w["n_months"]) if w else None,
                "peak share": round(float(w["peak_probability"]), 2) if w else None,
                "labels at station": o.get("n_labels_station"),
                "latest label": o.get("last_label_date"),
            }
        )
    if not rows:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(rows, columns=cols)
    return df.sort_values(["days until", "node"], na_position="last", kind="stable").reset_index(
        drop=True
    )


BASELINE_VERSIONS = {
    "persistence-v1": "persistence",
    "station-climatology-v1": "station climatology",
}
SIM_READING_TEXT = (
    "SIMULATED reading - random values, not stored, not a measurement. It shows how each model "
    "responds to its inputs, not what the river will do."
)


def ranking_frame(body: dict[str, Any], kind: str = "event") -> pd.DataFrame:
    """Backtest ranking of every predictor: walk-forward first, hold-out next to it.

    Sorted by the walk-forward headline metric, best first. `vs station climatology` is the
    report's walk-forward verdict, so a higher rank is not by itself a demonstrated win.
    """
    cols = ["rank", "predictor", "key", "algorithm", "is_baseline"]
    cols += ["walk-forward", "final hold-out", "vs station climatology"]
    info = body.get("kinds", {}).get(kind)
    if not info:
        return pd.DataFrame(columns=cols)
    wf = benchmark_frame(body, kind, "walk_forward")
    if wf.empty:
        return pd.DataFrame(columns=cols)
    hold = benchmark_frame(body, kind, "holdout")
    hold_value = dict(zip(hold["key"], hold["value"], strict=True)) if len(hold) else {}
    verdict = "vs station climatology"
    out = pd.DataFrame(
        {
            "predictor": wf["predictor"],
            "key": wf["key"],
            "algorithm": wf["algorithm"],
            "is_baseline": wf["is_baseline"],
            "walk-forward": wf["value"].round(3),
            "final hold-out": [
                round(hold_value[k], 3) if k in hold_value else None for k in wf["key"]
            ],
            verdict: wf[verdict].fillna("-") if verdict in wf else "-",
        }
    )
    out = out[out["key"] != "base_rate"]
    out = out.sort_values(
        "walk-forward", ascending=not info["higher_is_better"], kind="stable"
    ).reset_index(drop=True)
    out.insert(0, "rank", range(1, len(out) + 1))
    return out[cols]


def top_model(ranking: pd.DataFrame) -> dict[str, Any] | None:
    """The best-ranked trained model (baselines excluded), or None."""
    models = ranking[~ranking["is_baseline"].astype(bool)] if len(ranking) else ranking
    return models.iloc[0].to_dict() if len(models) else None


def winner_text(ranking: pd.DataFrame, metric: str = "PR-AUC") -> str:
    """One honest sentence: who ranks first and whether that is a demonstrated win."""
    best = top_model(ranking)
    if best is None:
        return "No trained model has been scored yet."
    text = (
        f"Top-ranked model: **{best['algorithm'].replace('_', ' ')}** "
        f"(walk-forward {metric} {best['walk-forward']:.3f}"
    )
    if pd.notna(best["final hold-out"]):
        text += f", final hold-out {best['final hold-out']:.3f}"
    text += ")."
    verdict = best["vs station climatology"]
    if verdict == "beats":
        return text + " It beats the station climatology baseline."
    if verdict == "is worse than":
        return text + " It is worse than the station climatology baseline."
    if verdict == "no demonstrable difference from":
        return text + (
            " It shows no demonstrable difference from the station climatology baseline, so it "
            "leads the ranking without being a proven winner."
        )
    return text


def version_label(version: str, models: list[dict[str, Any]]) -> tuple[str, str, bool]:
    """(display name, algorithm, is_baseline) for a stored `model_version`."""
    if version in BASELINE_VERSIONS:
        return BASELINE_VERSIONS[version], version, True
    for m in models:
        if m["version"] == version:
            return m["algorithm"].replace("_", " "), m["algorithm"], False
    return version, version, False
