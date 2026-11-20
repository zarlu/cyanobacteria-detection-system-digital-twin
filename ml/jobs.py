"""Explicit ML jobs of the demo loop: `train` (registry) and `forecast` (stored predictions).

    python -m ml.jobs train    --task crp_chla_demo
    python -m ml.jobs forecast --task crp_chla_demo

Both read their data from the database (what `/ingest` stored), not from the clean CSV.

train
    Fits every algorithm of the task on the training rows of the final hold-out fold (labels
    dated before hold-out start minus the gap), for every target kind, saves one joblib artifact
    per algorithm under `artifacts/` and writes one `models` row each. The stored metrics are
    copied from the task's latest harness report; nothing is re-scored here. The task's
    `deploy.algorithm` is registered as champion when the task has none (there is nothing to
    compare it with); the others are candidates, kept for the side-by-side view.
forecast
    For every station visit from the hold-out start on, stores the nowcast of every registered
    model (`p_event`, `yhat` with a split-conformal interval when the report has one) plus
    `persistence-v1` and `station-climatology-v1` at the same issue time.
    `issued_at = valid_at` = the visit time and `horizon_h = 0`: the task is a nowcast.
    Re-running changes nothing.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from db.engine import init_db, make_engine, make_session_factory
from db.models import Model, Prediction
from ml.benchmarks import load_metrics
from ml.config import REPO, TaskError, load_task
from ml.dbsource import read_long
from ml.features import build_features, previous_by_station
from ml.models import FoldModel
from ml.outlook import month_value
from ml.pipeline import predictor_key
from ml.source import build_frame, make_labels, transform_fn
from ml.splits import make_splits

log = logging.getLogger("ml.jobs")

ARTIFACTS = REPO / "artifacts"
PERSISTENCE_VERSION = "persistence-v1"
CLIMATOLOGY_VERSION = "station-climatology-v1"
NOWCAST_HORIZON_H = 0.0


def _deploy(task: dict[str, Any]) -> dict[str, Any]:
    dep = task.get("deploy")
    if not dep:
        raise TaskError(f"task {task.get('name')!r} has no `deploy` block")
    return dep


def db_frame(session: Session, task: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(frame as `build_frame` makes it, the long rows it came from) for the task's stations."""
    dep = _deploy(task)
    long = read_long(session, dep["db_source"], dep["node_prefix"])
    if long.empty:
        raise TaskError("the database holds no rows for this task; load the data first")
    frame, _ = build_frame(long, task)
    return frame, long


def _utc(ts: pd.Timestamp) -> datetime:
    return ts.to_pydatetime().replace(tzinfo=UTC)


def _report_metrics(task_name: str, key: str | None, kinds: list[str]) -> dict[str, Any] | None:
    """Headline numbers of `key` (a model key, or None for the baselines) from the report."""
    loaded = load_metrics(task_name)
    if loaded is None:
        return None
    metrics, path = loaded
    out: dict[str, Any] = {"report": f"ml/reports/{task_name}/{path.name}/report.md"}
    for kind in kinds:
        res = metrics["targets"].get(kind)
        if not res:
            continue
        for block in ("walk_forward", "holdout"):
            if not res.get(block):
                continue
            everything = res[block]["all"]
            preds = everything["predictors"]
            names = [key] if key else [k for k in preds if "@" not in k]
            out.setdefault(kind, {})[block] = {
                "n": everything.get("n"),
                "positives": everything.get("positives"),
                **{
                    name: {m: v.get("value") for m, v in preds[name]["metrics"].items()}
                    for name in names
                    if name in preds
                },
            }
    return out


def _fit_all(
    task: dict[str, Any], frame: pd.DataFrame, task_name: str
) -> tuple[dict[str, dict[str, FoldModel]], dict[str, Any]]:
    """Every algorithm of the task fitted on the hold-out fold's training rows, per kind."""
    set_name = _deploy(task)["feature_set"]
    loaded = load_metrics(task_name)
    fitted: dict[str, dict[str, FoldModel]] = {}
    info: dict[str, Any] = {"notes": {}, "folds": {}, "holdout_start": None}
    train_start = train_end = None
    for kind in task["targets"]:
        labels, _ = make_labels(frame, task, kind)
        plan = make_splits(labels, task["split"], kind)
        if plan.holdout is None:
            raise TaskError(f"{kind}: no hold-out fold ({plan.holdout_skipped})")
        fold = plan.holdout
        X = build_features(frame, task, set_name).loc[labels["row"]].reset_index(drop=True)
        y = labels["y"].to_numpy(dtype=float)
        info["folds"][kind] = fold.describe(labels, kind)
        notes: list[str] = []
        same = False
        conformal: dict[str, Any] = {}
        if loaded is not None:
            res = loaded[0]["targets"].get(kind, {})
            report_fold = res.get("holdout_fold") or {}
            same = all(
                info["folds"][kind].get(k) == report_fold.get(k) for k in info["folds"][kind]
            )
            notes.append(
                f"{kind}: the database training fold "
                + ("matches" if same else "DIFFERS from")
                + f" the report's hold-out fold (n_train {info['folds'][kind]['n_train']} vs "
                f"{report_fold.get('n_train')})."
            )
            conformal = res.get("conformal") or {}
        info["notes"][kind] = notes
        for algo, params in task["models"][kind].items():
            model = FoldModel(algo, kind, params or {}, int(task["seed"]))
            model.fit(X.iloc[fold.train_idx], y[fold.train_idx], X.iloc[fold.test_idx])
            key = predictor_key(algo, set_name)
            half = (conformal.get("predictors") or {}).get(key, {}).get("half_width_transformed")
            if kind == "regression" and half is not None and same:
                model.q_ = float(half)
                model.n_residuals_ = int(conformal.get("n_residuals") or 0)
            fitted.setdefault(algo, {})[kind] = model
        tr_dates = labels["date"].iloc[fold.train_idx]
        train_start = tr_dates.min() if train_start is None else min(train_start, tr_dates.min())
        train_end = tr_dates.max() if train_end is None else max(train_end, tr_dates.max())
        if kind == task["targets"][0]:
            info["holdout_start"] = plan.holdout_start
    info["train_start"], info["train_end"] = train_start, train_end
    return fitted, info


def train(
    session: Session,
    task_name: str,
    artifacts: Path = ARTIFACTS,
    task: dict[str, Any] | None = None,
) -> list[Model]:
    """Fit and register every algorithm of the task; returns the rows, champion first.

    `task` overrides the YAML of `task_name` (tests pass an invented, smaller spec).
    """
    task = task if task is not None else load_task(task_name)
    dep = _deploy(task)
    set_name = dep["feature_set"]
    frame, _ = db_frame(session, task)
    fitted, info = _fit_all(task, frame, task_name)
    train_start, train_end = info["train_start"], info["train_end"]
    artifacts.mkdir(parents=True, exist_ok=True)
    champion = session.scalars(
        select(Model).where(Model.task == task_name, Model.status == "champion")
    ).first()
    rows: list[Model] = []
    for algo, models in fitted.items():
        key = predictor_key(algo, set_name)
        kinds = list(models)
        version = f"{task_name}-{algo}-{train_end:%Y%m%d}"
        path = artifacts / f"{version}.joblib"
        joblib.dump(
            {
                "version": version,
                "task": task,
                "feature_set": set_name,
                "models": models,
                "holdout_start": info["holdout_start"],
            },
            path,
        )
        notes = [n for kind in kinds for n in info["notes"][kind]]
        reg = models.get("regression")
        if reg is not None and reg.q_ is not None:
            notes.append(
                f"regression interval: split-conformal half-width {reg.q_:.4f} on the "
                f"transformed scale from {reg.n_residuals_} walk-forward residuals in the report."
            )
        row = session.get(Model, version)
        if row is None:
            row = Model(version=version, created_at=datetime.now(UTC))
            session.add(row)
            if algo == dep["algorithm"] and champion is None:
                row.status = "champion"
                notes.append(
                    "Registered as champion: the task's deploy.algorithm and the first model "
                    "of this task (nothing to compare with)."
                )
            else:
                row.status = "candidate"
                notes.append("Candidate, stored for comparison; the promotion job is not built.")
        else:
            notes.append(f"Re-trained in place; status {row.status} kept.")
        row.task = task_name
        row.algorithm = algo
        row.target = task["target"]["channel"]
        row.horizon_h = NOWCAST_HORIZON_H
        row.features_json = json.dumps(
            {
                "feature_set": set_name,
                "kinds": kinds,
                "columns": list(models[kinds[0]].imputer_.columns_),
            }
        )
        row.train_start, row.train_end = _utc(train_start), _utc(train_end)
        row.train_sources = dep["db_source"]
        row.n_train = int(info["folds"][kinds[0]]["n_train"])
        row.metrics_json = json.dumps(_report_metrics(task_name, key, kinds))
        row.baseline_metrics_json = json.dumps(_report_metrics(task_name, None, kinds))
        row.artifact_path = str(path.relative_to(REPO)) if path.is_relative_to(REPO) else str(path)
        row.notes = " ".join(notes)
        rows.append(row)
    session.commit()
    rows.sort(key=lambda r: r.status != "champion")
    log.info("trained %s", [(r.version, r.status) for r in rows])
    return rows


def load_bundle(row: Model) -> dict[str, Any]:
    artifact = Path(row.artifact_path)
    return joblib.load(artifact if artifact.is_absolute() else REPO / artifact)


def registered(session: Session, task_name: str) -> list[Model]:
    """Champion first, then candidates; raises when the task has no champion."""
    rows = session.scalars(
        select(Model).where(Model.task == task_name, Model.status.in_(("champion", "candidate")))
    ).all()
    rows = sorted(rows, key=lambda r: (r.status != "champion", r.version))
    if not rows or rows[0].status != "champion":
        raise TaskError(f"no champion for task {task_name!r}; run the train job first")
    return rows


def predict_bundle(
    bundle: dict[str, Any], X: pd.DataFrame
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(p_event, yhat, lo, hi) in original units; NaN where the bundle has no such model."""
    task, models, n = bundle["task"], bundle["models"], len(X)
    nan = np.full(n, np.nan)
    p_event = models["event"].predict(X) if "event" in models else nan
    yhat, lo, hi = nan, nan, nan
    if "regression" in models:
        _, inverse = transform_fn(task["target"].get("regression", {}).get("transform"))
        reg = models["regression"]
        yhat = inverse(reg.predict(X))
        if reg.q_ is not None:
            lo_t, hi_t = reg.predict_interval(X)
            lo, hi = inverse(lo_t), inverse(hi_t)
    return p_event, yhat, lo, hi


def climatology_values(
    task: dict[str, Any], frame: pd.DataFrame, before: pd.Timestamp, stations, months
) -> tuple[np.ndarray, np.ndarray]:
    """station_climatology (p_event, yhat) per (station, month), from labels before `before`."""
    clim = task.get("climatology", {})
    window, min_labels = int(clim.get("window_months", 1)), int(clim.get("min_labels", 3))
    out: dict[str, np.ndarray] = {}
    for kind in ("event", "regression"):
        values = np.full(len(stations), np.nan)
        if kind in task["targets"]:
            labels, _ = make_labels(frame, task, kind)
            labels = labels[labels["date"] < before]
            if len(labels):
                values = np.array(
                    [
                        month_value(labels, str(s), int(m), window, min_labels)["probability"]
                        for s, m in zip(stations, months, strict=True)
                    ],
                    dtype=float,
                )
                if kind == "regression":
                    _, inverse = transform_fn(task["target"].get("regression", {}).get("transform"))
                    values = inverse(values)
        out[kind] = values
    return out["event"], out["regression"]


def _insert(session: Session, rows: list[dict[str, Any]]) -> int:
    inserted = 0
    for start in range(0, len(rows), 500):
        stmt = (
            sqlite_insert(Prediction)
            .values(rows[start : start + 500])
            .on_conflict_do_nothing(
                index_elements=["model_version", "node_id", "target", "issued_at", "horizon_h"]
            )
        )
        inserted += session.execute(stmt).rowcount
    return inserted


def forecast(session: Session, task_name: str) -> dict[str, Any]:
    rows_models = registered(session, task_name)
    champion = rows_models[0]
    bundles = {m.version: load_bundle(m) for m in rows_models}
    first = bundles[champion.version]
    task = first["task"]
    dep = _deploy(task)
    target = task["target"]["channel"]
    frame, long = db_frame(session, task)
    visits = frame[frame["date"] >= first["holdout_start"]]
    if visits.empty:
        return {"visits": 0, "inserted": 0}
    X = build_features(frame, task, first["feature_set"]).loc[visits.index]

    long = long[long["quality"] == "good"].copy()
    long["date"] = long["ts_utc"].dt.tz_convert(None).dt.normalize()
    visit_ts = long.groupby(["station", "date"])["ts_utc"].min()
    issued = [
        visit_ts[(s, d)].to_pydatetime()
        for s, d in zip(visits["station"], visits["date"], strict=True)
    ]
    nodes = [f"{dep['node_prefix']}{s}" for s in visits["station"]]
    n = len(visits)
    nan = np.full(n, np.nan)
    series: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {
        version: predict_bundle(bundle, X) for version, bundle in bundles.items()
    }

    # persistence-v1: the previous label at the same station on a strictly earlier date.
    prev, _ = previous_by_station(frame["station"], frame["date"], frame[target])
    prev = prev.loc[visits.index].to_numpy(dtype=float)
    reg_labels, _ = make_labels(frame, task, "regression")
    before = reg_labels[reg_labels["date"] < first["holdout_start"]]
    threshold = task["target"].get("event_threshold")
    fallback_value = float(before["value"].median()) if len(before) else np.nan
    p_prev = nan
    if threshold is not None:
        base_rate = float((before["value"] >= float(threshold)).mean()) if len(before) else np.nan
        p_prev = np.where(np.isnan(prev), base_rate, (prev >= float(threshold)).astype(float))
    series[PERSISTENCE_VERSION] = (p_prev, np.where(np.isnan(prev), fallback_value, prev), nan, nan)

    # station-climatology-v1: season and station only, from the labels the models trained on.
    gap = pd.Timedelta(days=int(task["split"].get("gap_days", 0)))
    p_clim, y_clim = climatology_values(
        task, frame, first["holdout_start"] - gap, visits["station"], visits["date"].dt.month
    )
    series[CLIMATOLOGY_VERSION] = (p_clim, y_clim, nan, nan)

    def num(v: float) -> float | None:
        return None if v is None or not np.isfinite(v) else float(v)

    rows: list[dict[str, Any]] = []
    for version, (p_event, yhat, lo, hi) in series.items():
        for i in range(n):
            rows.append(
                {
                    "issued_at": issued[i],
                    "valid_at": issued[i],
                    "horizon_h": NOWCAST_HORIZON_H,
                    "node_id": nodes[i],
                    "target": target,
                    "yhat": num(yhat[i]),
                    "lo": num(lo[i]),
                    "hi": num(hi[i]),
                    "p_event": num(p_event[i]),
                    "model_version": version,
                }
            )
    inserted = _insert(session, rows)
    session.commit()
    out = {
        "champion": champion.version,
        "model_versions": list(series),
        "visits": int(n),
        "rows": len(rows),
        "inserted": int(inserted),
        "already_stored": len(rows) - int(inserted),
        "first_visit": str(visits["date"].min().date()),
        "last_visit": str(visits["date"].max().date()),
    }
    log.info("forecast %s", out)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="ML jobs: train, forecast")
    p.add_argument("job", choices=["train", "forecast"])
    p.add_argument("--task", required=True)
    p.add_argument("--db-url", default=None, help="default: DB_URL env")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    engine = make_engine(args.db_url)
    init_db(engine)
    try:
        with make_session_factory(engine)() as session:
            if args.job == "train":
                rows = train(session, args.task)
                keys = ("version", "status", "n_train", "notes")
                print(json.dumps([{k: getattr(r, k) for k in keys} for r in rows], indent=2))
            else:
                print(json.dumps(forecast(session, args.task), indent=2))
    except TaskError as exc:
        log.error("%s", exc)
        return 2
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
