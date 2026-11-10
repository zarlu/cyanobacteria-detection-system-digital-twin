"""A4. Baselines. Every model result is reported next to these (CLAUDE.md rule 3).

All baselines predict in the target space `y` of the label table: a probability for event
targets, the transformed value (for example log1p) for regression targets.

persistence
    "The last sample at this station, carried forward": the previous label at the same station
    on a strictly earlier date. When a station has no earlier label, the training base rate
    (event) or the training median (regression) is used.
seasonal_climatology
    The mean of the training targets whose calendar month is within `window_months` of the test
    month (wrapping December to January), pooled over the task's stations. With fewer than
    `min_labels` training labels in that window, the mean of all training targets.
base_rate (event only)
    The training positive rate.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ml.features import previous_by_station

MEANING = {
    "persistence": (
        "the last sample at this station, carried forward (the previous label at the same "
        "station on a strictly earlier date; with no earlier label, the training {fallback})"
    ),
    "seasonal_climatology": (
        "the mean of the training targets within plus or minus {window} calendar month(s) of "
        "the test month, pooled over the task's stations (with fewer than {min_labels} training "
        "labels in that window, the mean of all training targets)"
    ),
    "base_rate": "the share of positives among the training labels",
}


def meaning(name: str, kind: str, cfg: dict[str, Any]) -> str:
    clim = cfg.get("climatology", {})
    return MEANING[name].format(
        fallback="base rate" if kind == "event" else "median",
        window=int(clim.get("window_months", 1)),
        min_labels=int(clim.get("min_labels", 3)),
    )


def persistence(
    labels: pd.DataFrame, train_idx: np.ndarray, test_idx: np.ndarray, kind: str
) -> np.ndarray:
    prev, _ = previous_by_station(labels["station"], labels["date"], labels["y"])
    y_train = labels["y"].to_numpy()[train_idx]
    fallback = float(np.mean(y_train)) if kind == "event" else float(np.median(y_train))
    out = prev.to_numpy(dtype=float)[test_idx]
    return np.where(np.isnan(out), fallback, out)


def month_distance(a: np.ndarray, b: int) -> np.ndarray:
    """Circular distance in calendar months (December and January are 1 apart)."""
    d = np.abs(np.asarray(a) - b) % 12
    return np.minimum(d, 12 - d)


def seasonal_climatology(
    labels: pd.DataFrame,
    train_idx: np.ndarray,
    test_idx: np.ndarray,
    window_months: int = 1,
    min_labels: int = 3,
) -> np.ndarray:
    months = labels["date"].dt.month.to_numpy()
    y = labels["y"].to_numpy(dtype=float)
    train_months, y_train = months[train_idx], y[train_idx]
    overall = float(np.mean(y_train))
    out = np.empty(len(test_idx), dtype=float)
    for i, month in enumerate(months[test_idx]):
        near = month_distance(train_months, int(month)) <= window_months
        out[i] = float(np.mean(y_train[near])) if int(near.sum()) >= min_labels else overall
    return out


def base_rate(labels: pd.DataFrame, train_idx: np.ndarray, test_idx: np.ndarray) -> np.ndarray:
    return np.full(len(test_idx), float(np.mean(labels["y"].to_numpy(dtype=float)[train_idx])))


def predict_baseline(
    name: str,
    labels: pd.DataFrame,
    train_idx: np.ndarray,
    test_idx: np.ndarray,
    kind: str,
    cfg: dict[str, Any],
) -> np.ndarray:
    if name == "persistence":
        return persistence(labels, train_idx, test_idx, kind)
    if name == "seasonal_climatology":
        clim = cfg.get("climatology", {})
        return seasonal_climatology(
            labels,
            train_idx,
            test_idx,
            int(clim.get("window_months", 1)),
            int(clim.get("min_labels", 3)),
        )
    if name == "base_rate":
        if kind != "event":
            raise ValueError("base_rate is defined for event targets only")
        return base_rate(labels, train_idx, test_idx)
    raise ValueError(f"unknown baseline {name!r}")
