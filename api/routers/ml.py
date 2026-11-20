"""ML read endpoints: registry, benchmarks, stored predictions and the seasonal outlook.

Nothing here fits or scores a model: benchmarks are copied from the harness reports under
``ml/reports/``, predictions are rows the forecast job stored, and the outlook is the station
climatology of the stored labels (``ml.outlook``).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pandas as pd
from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from api.deps import SessionDep
from db.models import Model, Prediction
from ml.benchmarks import benchmarks, report_tasks
from ml.config import TaskError, load_task
from ml.dbsource import read_long
from ml.outlook import seasonal_outlook
from ml.source import build_frame, make_labels

router = APIRouter(tags=["ml"])


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _json(text: str | None) -> Any:
    return json.loads(text) if text else None


@router.get("/models")
def list_models(session: SessionDep, task: str | None = None) -> list[dict[str, Any]]:
    """Model registry rows, champions first."""
    stmt = select(Model).order_by(Model.status != "champion", Model.created_at.desc())
    if task:
        stmt = stmt.where(Model.task == task)
    return [
        {
            "version": m.version,
            "task": m.task,
            "algorithm": m.algorithm,
            "target": m.target,
            "horizon_h": m.horizon_h,
            "features": _json(m.features_json),
            "train_start": _aware(m.train_start),
            "train_end": _aware(m.train_end),
            "train_sources": m.train_sources,
            "n_train": m.n_train,
            "metrics": _json(m.metrics_json),
            "baseline_metrics": _json(m.baseline_metrics_json),
            "status": m.status,
            "notes": m.notes,
            "created_at": _aware(m.created_at),
        }
        for m in session.scalars(stmt).all()
    ]


@router.get("/benchmarks")
def list_benchmark_tasks() -> list[str]:
    """Tasks that have a harness report."""
    return report_tasks()


@router.get("/benchmarks/{task}")
def get_benchmarks(task: str) -> dict[str, Any]:
    """Every baseline and model of the task's latest report, with intervals and verdicts."""
    if task not in report_tasks():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no report for task '{task}'")
    return benchmarks(task)  # type: ignore[return-value]


@router.get("/predictions")
def get_predictions(
    session: SessionDep,
    node_id: str | None = None,
    target: str | None = None,
    model_version: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict[str, Any]:
    """Stored predictions as columnar JSON, ordered by ``valid_at``."""
    stmt = select(Prediction).order_by(Prediction.valid_at, Prediction.node_id)
    if node_id:
        stmt = stmt.where(Prediction.node_id == node_id)
    if target:
        stmt = stmt.where(Prediction.target == target)
    if model_version:
        stmt = stmt.where(Prediction.model_version == model_version)
    if start:
        stmt = stmt.where(Prediction.valid_at >= _aware(start))
    if end:
        stmt = stmt.where(Prediction.valid_at <= _aware(end))
    rows = session.scalars(stmt).all()
    cols = ("node_id", "target", "horizon_h", "yhat", "lo", "hi", "p_event", "model_version")
    out: dict[str, Any] = {"count": len(rows)}
    out["issued_at"] = [_aware(r.issued_at) for r in rows]
    out["valid_at"] = [_aware(r.valid_at) for r in rows]
    for c in cols:
        out[c] = [getattr(r, c) for r in rows]
    return out


@router.get("/outlook")
def get_outlook(session: SessionDep, task: str, node_id: str) -> dict[str, Any]:
    """Seasonal outlook for one node: the station climatology over the coming months.

    A statement about the calendar from the stored labels of the task's stations; current
    sensor readings do not move it.
    """
    try:
        spec = load_task(task)
    except (TaskError, FileNotFoundError) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"unknown task '{task}'") from exc
    dep = spec.get("deploy")
    if not dep or "event" not in spec["targets"]:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=f"task '{task}' has no deploy block or event target"
        )
    cfg = {**spec.get("climatology", {}), **dep.get("outlook", {})}
    prefix = dep["node_prefix"]
    station = node_id[len(prefix) :] if node_id.startswith(prefix) else None
    today = datetime.now(UTC).date()
    info = {
        "task": task,
        "node_id": node_id,
        "target": spec["target"]["channel"],
        "unit": spec["target"].get("unit"),
        "event_threshold": spec["target"]["event_threshold"],
        "labels_from": spec["start"],
        "source": dep["db_source"],
    }
    long = read_long(session, dep["db_source"], prefix, channels=[spec["target"]["channel"]])
    if station is None or station not in [str(s) for s in spec["data"]["stations"]] or long.empty:
        no_labels = pd.DataFrame({"station": [], "date": [], "y": []})
        return {**info, **seasonal_outlook(no_labels, station or "", today), "station": station}
    frame, _ = build_frame(long, spec)
    labels, _ = make_labels(frame, spec, "event")
    out = seasonal_outlook(
        labels,
        station,
        today,
        months_ahead=int(cfg.get("months_ahead", 12)),
        window_months=int(cfg.get("window_months", 1)),
        min_labels=int(cfg.get("min_labels", 3)),
        likely_probability=float(cfg.get("likely_probability", 0.5)),
    )
    mine = labels[labels["station"].astype(str) == station]
    out["last_label_date"] = str(mine["date"].max().date()) if len(mine) else None
    return {**info, **out}


@router.get("/simulate")
def simulate(session: SessionDep, task: str, node_id: str, seed: int = 0) -> dict[str, Any]:
    """One invented sensor reading for a node and what every registered model says about it.

    Nothing is stored. The reading is random (``source='simulator'``): each channel is drawn
    between the 5th and 95th percentile of the node's stored good readings. It shows how the
    models respond to their inputs; it says nothing about the river.
    """
    try:
        from ml.whatif import simulate_reading  # needs the optional `ml` extra
    except ImportError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the ML extra is not installed (run `make setup-ml`)",
        ) from exc
    try:
        return simulate_reading(session, task, node_id, seed, datetime.now(UTC).date())
    except (TaskError, FileNotFoundError) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
