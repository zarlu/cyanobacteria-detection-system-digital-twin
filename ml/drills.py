"""A7. Leakage defenses, printed in every report under "Leakage checks".

(a) truncation_check: the features of every issue date are identical when all rows dated after
    it are removed.
(b) shuffled_label_check: with the training labels permuted, out-of-sample skill must fall to
    the no-skill level; otherwise the report says LEAKAGE SUSPECTED.
(c) assert_no_future_dependence: the guard built on (a). A deliberately leaky feature (the next
    sample's target) must make it raise; tests/ml/test_drills.py asserts that it does.
(d) invalid_random_split_demo: the same models scored with a random split of the rows. It is
    printed as "what a random split would have claimed; NOT a valid estimate" and is the only
    place in ml/ where a random split may exist (a test greps for it).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ml.eval import average_precision, mae, roc_auc
from ml.features import build_features
from ml.pipeline import run_folds
from ml.splits import Fold

LEAKAGE_SUSPECTED = "LEAKAGE SUSPECTED"


class LeakageError(Exception):
    """A feature depends on rows dated after its issue date."""


def truncation_check(
    frame: pd.DataFrame, task: dict[str, Any], set_name: str, max_examples: int = 5
) -> dict[str, Any]:
    """Rebuild the features of every label date from the rows dated at or before it and compare
    them with the features built from the full table."""
    target = task["target"]["channel"]
    full = build_features(frame, task, set_name)
    issue_dates = np.sort(frame.loc[frame[target].notna(), "date"].unique())
    mismatched: list[dict[str, Any]] = []
    rows_checked = 0
    for d in issue_dates:
        past = frame[frame["date"] <= d]
        idx = past.index[(past["date"] == d).to_numpy()]
        truncated = build_features(past, task, set_name).loc[idx]
        expected = full.loc[idx]
        rows_checked += len(idx)
        same = np.isclose(
            truncated.to_numpy(dtype=float),
            expected.to_numpy(dtype=float),
            rtol=0.0,
            atol=1e-12,
            equal_nan=True,
        )
        if not same.all():
            bad = [str(c) for c in full.columns[~same.all(axis=0)]]
            mismatched.append({"date": str(pd.Timestamp(d).date()), "features": bad})
    return {
        "feature_set": set_name,
        "issue_dates_checked": int(len(issue_dates)),
        "rows_checked": int(rows_checked),
        "dates_with_mismatch": int(len(mismatched)),
        "examples": mismatched[:max_examples],
        "passed": not mismatched,
    }


def assert_no_future_dependence(frame: pd.DataFrame, task: dict[str, Any], set_name: str) -> None:
    """Guard: raise LeakageError when any feature changes after later rows are removed."""
    result = truncation_check(frame, task, set_name)
    if not result["passed"]:
        raise LeakageError(
            f"feature set {set_name!r} uses rows dated after the issue date on "
            f"{result['dates_with_mismatch']} date(s); first: {result['examples'][0]}"
        )


def judge_shuffle(kind: str, mean_score: float, reference: float, margin: float) -> bool:
    """True when a shuffled-label run still shows skill (leakage suspected).

    event: mean PR-AUC exceeds the base rate by more than `margin`.
    regression: mean MAE skill against the training-mean predictor exceeds `margin` (a
    squared-loss model fitted on shuffled targets estimates the training mean).
    """
    if not np.isfinite(mean_score) or not np.isfinite(reference):
        return False
    if kind == "event":
        return bool(mean_score > reference + margin)
    return bool(reference > 0 and 1.0 - mean_score / reference > margin)


def shuffled_label_check(
    labels: pd.DataFrame,
    X: dict[str, pd.DataFrame],
    folds: list[Fold],
    task: dict[str, Any],
    kind: str,
) -> dict[str, Any]:
    """Refit every model with the training labels of each fold permuted (fixed seeds) and score
    it on the real out-of-sample labels."""
    cfg = task["drills"]
    seeds = [int(s) for s in cfg["shuffle_seeds"]]
    margin = float(cfg["shuffle_margin"])
    if not folds:
        return {"ran": False, "reason": "no walk-forward fold"}
    y = labels["y"].to_numpy(dtype=float)
    rows = np.concatenate([f.test_idx for f in folds])
    y_test = y[rows]
    context: dict[str, Any] = {}
    if kind == "event":
        reference = float(y_test.mean())
        reference_name = "base rate of the out-of-sample labels"
        metric = "PR-AUC"
        # Context, not part of the rule: PR-AUC of a ranking that ignores the labels is above the
        # base rate on small samples. Estimated by permuting the test labels (fixed seed).
        rng = np.random.default_rng(int(cfg.get("label_blind_seed", 0)))
        ranking = np.arange(len(y_test), dtype=float)
        draws = [average_precision(rng.permutation(y_test), ranking) for _ in range(2000)]
        context["label_blind_expected_pr_auc"] = float(np.mean(draws))
    else:
        mean_pred = np.concatenate(
            [np.full(len(f.test_idx), np.mean(y[f.train_idx])) for f in folds]
        )
        reference = mae(y_test, mean_pred)
        reference_name = "MAE of the training-mean predictor (transformed scale)"
        metric = "MAE (transformed scale)"

    per_seed: dict[str, list[float]] = {}
    per_seed_roc: dict[str, list[float]] = {}
    for seed in seeds:
        rng = np.random.default_rng(seed)
        run = run_folds(
            labels,
            X,
            folds,
            task,
            kind,
            with_baselines=False,
            train_y=lambda _fold, y_train, rng=rng: rng.permutation(y_train),
        )
        for key, pred in run.preds.items():
            score = average_precision(y_test, pred) if kind == "event" else mae(y_test, pred)
            per_seed.setdefault(key, []).append(float(score))
            if kind == "event":
                per_seed_roc.setdefault(key, []).append(roc_auc(y_test, pred))

    predictors = {}
    for key, scores in per_seed.items():
        mean_score = float(np.mean(scores))
        predictors[key] = {
            "mean": mean_score,
            "per_seed": scores,
            "suspected": judge_shuffle(kind, mean_score, reference, margin),
        }
        if kind == "event":
            predictors[key]["mean_roc_auc"] = float(np.mean(per_seed_roc[key]))
    return {
        "ran": True,
        "metric": metric,
        "reference": reference,
        "reference_name": reference_name,
        "margin": margin,
        "seeds": seeds,
        "n": int(len(rows)),
        **context,
        "predictors": predictors,
        "suspected": any(p["suspected"] for p in predictors.values()),
    }


def invalid_random_split_demo(
    labels: pd.DataFrame,
    X: dict[str, pd.DataFrame],
    rows: np.ndarray,
    task: dict[str, Any],
    kind: str,
) -> dict[str, Any]:
    """What a random split would have claimed; NOT a valid estimate.

    Scores the models of the headline feature set with a random K-fold over `rows`. Future
    samples train the model that predicts past ones, so the result is optimistic by
    construction. Nothing else in ml/ may split at random.
    """
    from sklearn.model_selection import KFold

    cfg = task["drills"]
    n_splits = int(cfg["random_split_folds"])
    rows = np.asarray(rows, dtype=int)
    if len(rows) < n_splits:
        return {"ran": False, "reason": f"fewer than {n_splits} rows"}
    splitter = KFold(n_splits=n_splits, shuffle=True, random_state=int(cfg["random_split_seed"]))
    boundary = pd.Timestamp(labels["date"].min())
    folds = [
        Fold(f"random-{i}", rows[tr], rows[te], boundary, 0)
        for i, (tr, te) in enumerate(splitter.split(rows))
    ]
    set_name = task["headline_feature_set"]
    run = run_folds(labels, X, folds, task, kind, with_baselines=False, feature_sets=[set_name])
    y_test = labels["y"].to_numpy(dtype=float)[run.rows]
    out = {}
    for key, pred in run.preds.items():
        out[key] = float(average_precision(y_test, pred) if kind == "event" else mae(y_test, pred))
    return {
        "ran": True,
        "label": "what a random split would have claimed; NOT a valid estimate",
        "metric": "PR-AUC" if kind == "event" else "MAE (transformed scale)",
        "n": int(len(rows)),
        "n_splits": n_splits,
        "predictors": out,
    }
