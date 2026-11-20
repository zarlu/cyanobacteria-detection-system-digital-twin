"""What-if: an invented sensor reading and the response of every registered model.

`simulate_reading` draws one random value per input channel of the task's deployed feature
set, between the 5th and 95th percentile of the station's stored good readings (all the task's
stations when the station has fewer than `MIN_VALUES`), dates it today and asks every
registered model for its nowcast. Nothing is stored. The reading is simulated: it shows how the
models respond to their inputs, not what the river will do.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from contract.registry import load_registry
from ml.config import TaskError, resolve_feature_set
from ml.features import build_features
from ml.jobs import (
    CLIMATOLOGY_VERSION,
    climatology_values,
    db_frame,
    load_bundle,
    predict_bundle,
    registered,
)

MIN_VALUES = 5
LOW_Q, HIGH_Q = 0.05, 0.95


def input_channels(task: dict[str, Any], set_name: str) -> list[str]:
    specs, _ = resolve_feature_set(task, set_name)
    return [spec["channel"] for spec in specs if spec.get("kind") == "channel"]


def draw_reading(
    frame: pd.DataFrame, station: str, channels: list[str], rng: np.random.Generator
) -> list[dict[str, Any]]:
    """One random value per channel, inside the central 90% of the stored good readings."""
    registry = load_registry()
    mine = frame[frame["station"].astype(str) == str(station)]
    out = []
    for channel in channels:
        values = mine[channel].dropna() if channel in mine else pd.Series(dtype=float)
        basis = "station"
        if len(values) < MIN_VALUES:
            values = frame[channel].dropna() if channel in frame else values
            basis = "all_stations"
        if values.empty:
            continue
        low, high = (float(values.quantile(q)) for q in (LOW_Q, HIGH_Q))
        out.append(
            {
                "channel": channel,
                "value": round(float(rng.uniform(low, high)), 2),
                "unit": registry[channel].unit if channel in registry else None,
                "drawn_between": [round(low, 2), round(high, 2)],
                "basis": basis,
            }
        )
    return out


def _num(v: float) -> float | None:
    return None if v is None or not np.isfinite(v) else float(v)


def simulate_reading(
    session: Session, task_name: str, node_id: str, seed: int, today: date
) -> dict[str, Any]:
    models = registered(session, task_name)
    bundles = {m.version: load_bundle(m) for m in models}
    first = bundles[models[0].version]
    task = first["task"]
    prefix = task["deploy"]["node_prefix"]
    station = node_id[len(prefix) :] if node_id.startswith(prefix) else None
    if station is None or station not in [str(s) for s in task["data"]["stations"]]:
        raise TaskError(f"node {node_id!r} is not a station of task {task_name!r}")
    frame, _ = db_frame(session, task)
    channels = input_channels(task, first["feature_set"])
    readings = draw_reading(frame, station, channels, np.random.default_rng(seed))

    target = task["target"]["channel"]
    row: dict[str, Any] = {"station": station, "date": pd.Timestamp(today), target: np.nan}
    row.update({f"{target}__censored": None, f"{target}__dl": np.nan})
    row.update({c: np.nan for c in channels})
    row.update({r["channel"]: r["value"] for r in readings})
    one = pd.DataFrame([row])
    X = build_features(one, task, first["feature_set"])

    predictions = []
    for m in models:
        p_event, yhat, lo, hi = predict_bundle(bundles[m.version], X)
        predictions.append(
            {
                "model_version": m.version,
                "algorithm": m.algorithm,
                "status": m.status,
                "uses_reading": True,
                "p_event": _num(p_event[0]),
                "yhat": _num(yhat[0]),
                "lo": _num(lo[0]),
                "hi": _num(hi[0]),
            }
        )
    gap = pd.Timedelta(days=int(task["split"].get("gap_days", 0)))
    p_clim, y_clim = climatology_values(
        task, frame, first["holdout_start"] - gap, [station], [today.month]
    )
    predictions.append(
        {
            "model_version": CLIMATOLOGY_VERSION,
            "algorithm": "station_climatology",
            "status": "baseline",
            "uses_reading": False,
            "p_event": _num(p_clim[0]),
            "yhat": _num(y_clim[0]),
            "lo": None,
            "hi": None,
        }
    )
    return {
        "source": "simulator",
        "stored": False,
        "task": task_name,
        "node_id": node_id,
        "seed": int(seed),
        "date": today.isoformat(),
        "target": target,
        "unit": task["target"].get("unit"),
        "event_threshold": task["target"].get("event_threshold"),
        "readings": readings,
        "predictions": predictions,
    }
