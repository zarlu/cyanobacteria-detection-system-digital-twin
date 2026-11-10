"""A5. The model ladder behind one interface: fit, predict, predict_interval, explain.

Event targets: logistic regression (L2, standardized inputs), RandomForest classifier, LightGBM
classifier. Regression targets (fit on the transformed target, for example log1p): ridge
(standardized inputs), RandomForest regressor, LightGBM regressor. Every setting comes from the
task YAML; there is no hyperparameter search.

`predict` returns the probability of the event, or the prediction on the transformed scale.

Intervals (regression only) are split-conformal: the half-width is the
ceil((n + 1) * level) / n empirical quantile of the absolute walk-forward residuals on the
transformed scale, so the interval is `prediction +/- q` there. With too few residuals the
quantile does not exist and the interval is infinite.

`explain` is permutation importance: the loss in score when one raw feature column is shuffled
among the rows it is given (the walk-forward test rows; nothing is refit).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier, LGBMRegressor
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.preprocessing import StandardScaler

from ml.features import FoldImputer

LGBM_FIXED = {"verbose": -1, "n_jobs": 1, "deterministic": True, "force_row_wise": True}


def _estimator(kind: str, algo: str, params: dict[str, Any], seed: int) -> tuple[Any, bool]:
    """(estimator, needs standardized inputs)."""
    p = dict(params)
    if kind == "event":
        if algo == "logistic":
            return LogisticRegression(max_iter=5000, random_state=seed, **p), True
        if algo == "random_forest":
            return RandomForestClassifier(random_state=seed, n_jobs=1, **p), False
        if algo == "lightgbm":
            return LGBMClassifier(random_state=seed, **LGBM_FIXED, **p), False
    if kind == "regression":
        if algo == "ridge":
            return Ridge(random_state=seed, **p), True
        if algo == "random_forest":
            return RandomForestRegressor(random_state=seed, n_jobs=1, **p), False
        if algo == "lightgbm":
            return LGBMRegressor(random_state=seed, **LGBM_FIXED, **p), False
    raise ValueError(f"no algorithm {algo!r} for {kind} targets")


class FoldModel:
    """One algorithm fitted on one training fold (imputer and scaler included)."""

    def __init__(self, algo: str, kind: str, params: dict[str, Any], seed: int):
        self.algo, self.kind, self.params, self.seed = algo, kind, dict(params), int(seed)
        self.q_: float | None = None
        self.n_residuals_ = 0

    def fit(self, X: pd.DataFrame, y: np.ndarray, X_other: pd.DataFrame | None = None):
        """Fit on the training rows. `X_other` (the fold's test rows) only decides which
        missing-indicator columns exist; see FoldImputer."""
        y = np.asarray(y, dtype=float)
        self.imputer_ = FoldImputer().fit(X, X_other)
        Z = self.imputer_.transform(X).to_numpy(dtype=float)
        self.constant_: float | None = None
        if self.kind == "event" and len(np.unique(y)) < 2:
            self.constant_ = float(y.mean())  # one class in train: predict its rate
            return self
        self.estimator_, scale = _estimator(self.kind, self.algo, self.params, self.seed)
        self.scaler_ = StandardScaler().fit(Z) if scale else None
        if self.scaler_ is not None:
            Z = self.scaler_.transform(Z)
        self.estimator_.fit(Z, y.astype(int) if self.kind == "event" else y)
        return self

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        Z = self.imputer_.transform(X).to_numpy(dtype=float)
        if self.constant_ is not None:
            return np.full(len(Z), self.constant_)
        if self.scaler_ is not None:
            Z = self.scaler_.transform(Z)
        if self.kind == "event":
            return self.estimator_.predict_proba(Z)[:, 1]
        return np.asarray(self.estimator_.predict(Z), dtype=float)

    def calibrate(self, residuals: np.ndarray, level: float = 0.90) -> FoldModel:
        """Set the split-conformal half-width from out-of-sample residuals."""
        self.q_, self.n_residuals_ = conformal_quantile(residuals, level), int(len(residuals))
        return self

    def predict_interval(self, X: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        if self.kind != "regression":
            raise NotImplementedError("intervals are defined for regression targets only")
        if self.q_ is None:
            raise RuntimeError("call calibrate() with walk-forward residuals first")
        pred = self.predict(X)
        return pred - self.q_, pred + self.q_

    def explain(
        self,
        X: pd.DataFrame,
        y: np.ndarray,
        score: Callable[[np.ndarray, np.ndarray], float],
        n_repeats: int = 5,
        seed: int = 0,
    ) -> dict[str, float]:
        return permutation_importance(
            [(self, X, np.asarray(y, dtype=float))], score, n_repeats, seed
        )


def conformal_quantile(residuals: np.ndarray, level: float = 0.90) -> float:
    """ceil((n + 1) * level)-th smallest absolute residual; infinite when n is too small."""
    r = np.sort(np.abs(np.asarray(residuals, dtype=float)))
    n = len(r)
    k = math.ceil((n + 1) * level)
    return float(r[k - 1]) if 0 < k <= n else float("inf")


def permutation_importance(
    fitted: list[tuple[FoldModel, pd.DataFrame, np.ndarray]],
    score: Callable[[np.ndarray, np.ndarray], float],
    n_repeats: int = 5,
    seed: int = 0,
) -> dict[str, float]:
    """Mean drop in `score` (higher = better) over the pooled rows of all folds when one raw
    feature column is permuted within each fold's rows. Positive = the model relies on it.
    """
    y_all = np.concatenate([y for _, _, y in fitted])
    base = score(y_all, np.concatenate([m.predict(X) for m, X, _ in fitted]))
    columns = list(fitted[0][1].columns)
    rng = np.random.default_rng(seed)
    out: dict[str, float] = {}
    for col in columns:
        drops = []
        for _ in range(n_repeats):
            preds = []
            for model, X, _y in fitted:
                Xp = X.copy()
                Xp[col] = rng.permutation(Xp[col].to_numpy())
                preds.append(model.predict(Xp))
            drops.append(base - score(y_all, np.concatenate(preds)))
        out[col] = float(np.mean(drops))
    return out
