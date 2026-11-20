"""Fit every baseline and model on every fold and collect out-of-sample predictions.

Shared by the runner and the leakage drills so that a drill exercises the same code path as the
real evaluation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from ml.baselines import predict_baseline
from ml.eval import EvalSet
from ml.models import FoldModel
from ml.source import transform_fn
from ml.splits import Fold


def predictor_key(algo: str, set_name: str) -> str:
    return f"{algo}@{set_name}"


def split_key(key: str) -> tuple[str, str | None]:
    algo, _, set_name = key.partition("@")
    return algo, (set_name or None)


@dataclass
class FoldRun:
    rows: np.ndarray  # label positions of the test rows, in fold order
    fold_of: np.ndarray  # fold name of each row
    preds: dict[str, np.ndarray]  # predictions in y-space, aligned with `rows`
    fitted: dict[str, list[tuple[FoldModel, np.ndarray]]] = field(default_factory=dict)


def run_folds(
    labels: pd.DataFrame,
    X: dict[str, pd.DataFrame],
    folds: list[Fold],
    task: dict[str, Any],
    kind: str,
    *,
    with_baselines: bool = True,
    feature_sets: list[str] | None = None,
    train_y: Callable[[Fold, np.ndarray], np.ndarray] | None = None,
    keep_models: bool = False,
) -> FoldRun:
    """Out-of-sample predictions of every predictor over `folds`.

    `X[set_name]` is aligned with `labels` (same RangeIndex). `train_y`, used only by the
    shuffled-label drill, replaces the training targets of a fold before model fitting.
    """
    y = labels["y"].to_numpy(dtype=float)
    sets = feature_sets if feature_sets is not None else list(task["use_feature_sets"])
    algos: dict[str, dict[str, Any]] = task["models"][kind]
    seed = int(task["seed"])
    base_names = list(task["baselines"][kind]) if with_baselines else []
    chunks: dict[str, list[np.ndarray]] = {}
    fitted: dict[str, list[tuple[FoldModel, np.ndarray]]] = {}

    for fold in folds:
        tr, te = fold.train_idx, fold.test_idx
        for name in base_names:
            chunks.setdefault(name, []).append(predict_baseline(name, labels, tr, te, kind, task))
        y_train = y[tr] if train_y is None else np.asarray(train_y(fold, y[tr]), dtype=float)
        for set_name in sets:
            X_train, X_test = X[set_name].iloc[tr], X[set_name].iloc[te]
            for algo, params in algos.items():
                key = predictor_key(algo, set_name)
                model = FoldModel(algo, kind, params or {}, seed).fit(X_train, y_train, X_test)
                chunks.setdefault(key, []).append(model.predict(X_test))
                if keep_models:
                    fitted.setdefault(key, []).append((model, te))

    if not folds:
        return FoldRun(np.array([], dtype=int), np.array([], dtype=str), {}, {})
    rows = np.concatenate([f.test_idx for f in folds])
    fold_of = np.concatenate([np.full(len(f.test_idx), f.name) for f in folds])
    preds = {k: np.concatenate(v) for k, v in chunks.items()}
    return FoldRun(rows, fold_of, preds, fitted)


def make_evalset(labels: pd.DataFrame, run: FoldRun, task: dict[str, Any], kind: str) -> EvalSet:
    sub = labels.iloc[run.rows]
    common = {
        "kind": kind,
        "dates": sub["date"].to_numpy().astype("datetime64[D]"),
        "stations": sub["station"].to_numpy().astype(str),
        "y": sub["y"].to_numpy(dtype=float),
    }
    if kind == "event":
        return EvalSet(preds={k: np.clip(v, 0.0, 1.0) for k, v in run.preds.items()}, **common)
    forward, inverse = transform_fn(task["target"].get("regression", {}).get("transform"))
    preds_value = {k: inverse(v) for k, v in run.preds.items()}
    return EvalSet(
        preds={k: forward(v) for k, v in preds_value.items()},
        value=sub["value"].to_numpy(dtype=float),
        preds_value=preds_value,
        censored=sub["censored"].to_numpy(dtype=bool),
        **common,
    )
