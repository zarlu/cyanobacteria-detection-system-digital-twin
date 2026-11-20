"""Seasonal bloom outlook: the station climatology laid out over the coming months.

For each of the next calendar months the outlook gives the share of past samples at the station,
within plus or minus `window_months` of that month, whose target reached the event threshold.
This is the `station_climatology` baseline of `ml.baselines` (same window, same fallbacks) with
every stored good label as history. It is a statement about the calendar, not a forecast driven
by current sensor readings: a new reading does not move it; a new label does.

`next_window` is the first run of months whose share is at or above `likely_probability`
(PLACEHOLDER, set in the task YAML under `deploy.outlook`).
"""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from ml.baselines import month_distance

BASIS_STATION = "station"
BASIS_POOLED = "pooled_stations"
BASIS_OVERALL = "all_months"


def _month_start(year: int, month: int, offset: int) -> date:
    index = year * 12 + (month - 1) + offset
    return date(index // 12, index % 12 + 1, 1)


def month_value(
    labels: pd.DataFrame, station: str, month: int, window_months: int, min_labels: int
) -> dict[str, Any]:
    """Station climatology for one calendar month, with the counts behind it."""
    months = labels["date"].dt.month.to_numpy()
    y = labels["y"].to_numpy(dtype=float)
    near = month_distance(months, month) <= window_months
    here = near & (labels["station"].to_numpy().astype(str) == str(station))
    if int(here.sum()) >= min_labels:
        chosen, basis = here, BASIS_STATION
    elif int(near.sum()) >= min_labels:
        chosen, basis = near, BASIS_POOLED
    else:
        chosen, basis = np.ones(len(labels), dtype=bool), BASIS_OVERALL
    return {
        "probability": float(np.mean(y[chosen])) if chosen.any() else None,
        "n_labels": int(chosen.sum()),
        "n_events": int(y[chosen].sum()),
        "basis": basis,
        "n_station_labels": int(here.sum()),
    }


def seasonal_outlook(
    labels: pd.DataFrame,
    station: str,
    today: date,
    *,
    months_ahead: int = 12,
    window_months: int = 1,
    min_labels: int = 3,
    likely_probability: float = 0.5,
) -> dict[str, Any]:
    """Outlook for `station` from the event label table (`station`, `date`, `y` in {0, 1})."""
    out: dict[str, Any] = {
        "station": str(station),
        "today": today.isoformat(),
        "likely_probability": float(likely_probability),
        "window_months": int(window_months),
        "min_labels": int(min_labels),
        "n_labels_station": int((labels["station"].astype(str) == str(station)).sum()),
        "n_labels_all": int(len(labels)),
        "months": [],
        "next_window": None,
    }
    if labels.empty:
        return out
    for i in range(months_ahead):
        start = _month_start(today.year, today.month, i)
        entry = month_value(labels, station, start.month, window_months, min_labels)
        first_day = today if i == 0 else start
        entry.update(
            {
                "month_start": start.isoformat(),
                "days_until": (first_day - today).days,
                "likely": entry["probability"] is not None
                and entry["probability"] >= likely_probability,
            }
        )
        out["months"].append(entry)
    likely = [m["likely"] for m in out["months"]]
    if any(likely):
        first = likely.index(True)
        last = first
        while last + 1 < len(likely) and likely[last + 1]:
            last += 1
        run = out["months"][first : last + 1]
        out["next_window"] = {
            "month_start": run[0]["month_start"],
            "days_until": run[0]["days_until"],
            "n_months": len(run),
            "last_month_start": run[-1]["month_start"],
            "peak_probability": max(m["probability"] for m in run),
            "basis": sorted({m["basis"] for m in run}),
        }
    return out
