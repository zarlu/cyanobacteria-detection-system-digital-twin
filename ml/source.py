"""A1. Read the cleaned long table and turn it into one row per (station, date) plus labels.

Only `quality == 'good'` rows of the configured `source` are used (CLAUDE.md rule 6). Real and
simulated data are never mixed: the task names exactly one source. Station ids come only from the
task YAML (rule 11).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ml.config import REPO, TaskError

REQUIRED_COLUMNS = (
    "ts_utc",
    "station",
    "channel",
    "value",
    "source",
    "quality",
    "censored",
    "detection_limit",
)


def data_path(task: dict[str, Any]) -> Path:
    path = Path(task["data"]["path"])
    return path if path.is_absolute() else REPO / path


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_long(path: Path) -> pd.DataFrame:
    if not path.is_file():
        raise TaskError(f"input file not found: {path}")
    long = pd.read_csv(path, dtype={"station": str}, low_memory=False)
    missing = [c for c in REQUIRED_COLUMNS if c not in long.columns]
    if missing:
        raise TaskError(f"{path}: missing columns {missing}")
    return long


def build_frame(long: pd.DataFrame, task: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Pivot good rows to one row per (station, date).

    Columns: `station`, `date` (UTC calendar date), one column per channel, and for the target
    channel `<channel>__censored` and `<channel>__dl`. A right-censored target value is not a
    usable label (the true value is only known to be above a limit): it is removed and counted.
    """
    data = task["data"]
    target = task["target"]["channel"]
    stations = [str(s) for s in data["stations"]]
    info: dict[str, Any] = {"rows_in_file": int(len(long))}

    rows = long[long["source"] == data["source_filter"]]
    info["rows_source"] = int(len(rows))
    rows = rows[rows["station"].isin(stations)]
    info["rows_stations"] = int(len(rows))
    good = rows[rows["quality"] == "good"].copy()
    info["rows_not_good_dropped"] = int(len(rows) - len(good))

    good["date"] = pd.to_datetime(good["ts_utc"], utc=True).dt.tz_convert(None).dt.normalize()
    start = pd.Timestamp(task["start"])
    info["rows_before_start_dropped"] = int((good["date"] < start).sum())
    good = good[good["date"] >= start]

    dup = good.duplicated(["station", "date", "channel"])
    if dup.any():
        raise TaskError(f"{int(dup.sum())} duplicate (station, date, channel) rows in the input")

    is_target = good["channel"] == target
    right = is_target & (good["censored"] == "right")
    info["target_right_censored_dropped"] = int(right.sum())
    good = good[~right]
    is_target = good["channel"] == target

    wide = good.pivot(index=["station", "date"], columns="channel", values="value")
    wide.columns.name = None
    tgt = good[is_target].set_index(["station", "date"])
    wide[f"{target}__censored"] = tgt["censored"].reindex(wide.index)
    wide[f"{target}__dl"] = tgt["detection_limit"].reindex(wide.index)
    if target not in wide.columns:
        wide[target] = np.nan
    frame = wide.reset_index().sort_values(["date", "station"], kind="stable")
    frame = frame.reset_index(drop=True)

    factor = float(task["target"].get("left_censored_factor", 0.5))
    left = frame[f"{target}__censored"] == "left"
    frame.loc[left, target] = frame.loc[left, f"{target}__dl"] * factor
    info["station_dates"] = int(len(frame))
    info["stations"] = stations
    return frame, info


def transform_fn(name: str | None):
    """(forward, inverse) for a target transform named in the task YAML."""
    if name in (None, "none"):
        return (lambda v: np.asarray(v, dtype=float)), (lambda v: np.asarray(v, dtype=float))
    if name == "log1p":
        return (
            lambda v: np.log1p(np.asarray(v, dtype=float)),
            lambda v: np.expm1(np.maximum(np.asarray(v, dtype=float), 0.0)),
        )
    raise TaskError(f"unknown target transform {name!r}")


def make_labels(
    frame: pd.DataFrame, task: dict[str, Any], kind: str
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Label table for one target kind, sorted by (date, station), with a RangeIndex.

    Columns: `row` (index into frame), `station`, `date`, `value` (original units),
    `censored` (bool, left-censored), `y` (0/1 for event, transformed value for regression).

    Event rules: a numeric value is compared with the threshold; a left-censored `<x` with
    x <= threshold is a safe negative; a left-censored `<x` with x > threshold is unknown and is
    dropped from the event task (counted in `event_unknown_censored_dropped`).
    """
    spec = task["target"]
    target = spec["channel"]
    rows = frame[frame[target].notna()]
    censored = (rows[f"{target}__censored"] == "left").to_numpy()
    dl = rows[f"{target}__dl"].to_numpy(dtype=float)
    value = rows[target].to_numpy(dtype=float)
    info: dict[str, Any] = {"labels_available": int(len(rows))}
    keep = np.ones(len(rows), dtype=bool)

    if kind == "event":
        threshold = float(spec["event_threshold"])
        unknown = censored & (dl > threshold)
        info["event_unknown_censored_dropped"] = int(unknown.sum())
        info["event_censored_safe_negatives"] = int((censored & ~unknown).sum())
        keep = ~unknown
        y = np.where(censored, 0.0, (value >= threshold).astype(float))
    elif kind == "regression":
        forward, _ = transform_fn(spec.get("regression", {}).get("transform"))
        y = forward(value)
    else:
        raise TaskError(f"unknown target kind {kind!r}")

    labels = pd.DataFrame(
        {
            "row": rows.index.to_numpy(),
            "station": rows["station"].to_numpy(),
            "date": rows["date"].to_numpy(),
            "value": value,
            "censored": censored,
            "y": y,
        }
    )[keep]
    labels = labels.sort_values(["date", "station"], kind="stable").reset_index(drop=True)
    info["labels_used"] = int(len(labels))
    info["labels_censored"] = int(labels["censored"].sum())
    info["distinct_dates"] = int(labels["date"].nunique())
    info["per_station"] = {str(k): int(v) for k, v in labels.groupby("station").size().items()}
    if kind == "event":
        info["positives"] = int(labels["y"].sum())
    return labels, info
