"""A7: leakage defenses."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("sklearn")
pytest.importorskip("lightgbm")

from ml import features  # noqa: E402
from ml.drills import (  # noqa: E402
    LeakageError,
    assert_no_future_dependence,
    invalid_random_split_demo,
    judge_shuffle,
    shuffled_label_check,
    truncation_check,
)
from ml.eval import average_precision  # noqa: E402
from ml.features import build_features  # noqa: E402
from ml.pipeline import run_folds  # noqa: E402
from ml.source import build_frame, load_long, make_labels  # noqa: E402
from ml.splits import make_splits  # noqa: E402


def _setup(tmp_path, make_long, make_task, kind="event", **long_kwargs):
    path = tmp_path / "drill.csv"
    make_long(**long_kwargs).to_csv(path, index=False)
    task = make_task(path)
    frame, _ = build_frame(load_long(path), task)
    labels, _ = make_labels(frame, task, kind)
    plan = make_splits(labels, task["split"], kind)
    X = {
        s: build_features(frame, task, s).loc[labels["row"]].reset_index(drop=True)
        for s in task["use_feature_sets"]
    }
    return task, frame, labels, plan, X


def test_truncation_features_do_not_change_when_later_rows_are_removed(
    tmp_path, make_long, make_task
):
    task, frame, labels, _, _ = _setup(tmp_path, make_long, make_task)
    for set_name in task["use_feature_sets"]:
        result = truncation_check(frame, task, set_name)
        assert result["passed"] and result["dates_with_mismatch"] == 0
        assert result["issue_dates_checked"] == frame["date"].nunique()
        assert result["rows_checked"] == len(frame)
        assert_no_future_dependence(frame, task, set_name)  # does not raise


def _next_label(frame, spec, ctx):
    """Deliberately leaky: the NEXT sample's target at the same station."""
    order = frame.sort_values(["station", "date"])
    nxt = order[ctx["target"]].groupby(order["station"]).shift(-1)
    return {spec["name"]: nxt.reindex(frame.index)}


def test_leaky_feature_trips_the_guard(tmp_path, make_long, make_task, monkeypatch):
    task, frame, _, _, _ = _setup(tmp_path, make_long, make_task)
    monkeypatch.setitem(features.FEATURE_KINDS, "next_label", _next_label)
    task["feature_sets"]["leaky"] = {
        "deployable": False,
        "extends": "sensor_like",
        "features": [{"name": "next_label", "kind": "next_label"}],
    }
    result = truncation_check(frame, task, "leaky")
    assert not result["passed"]
    assert result["dates_with_mismatch"] > 0.9 * result["issue_dates_checked"]
    assert result["examples"][0]["features"] == ["next_label"]  # only the leaky column is named
    with pytest.raises(LeakageError, match="leaky"):
        assert_no_future_dependence(frame, task, "leaky")


def test_shuffled_labels_fall_to_the_base_rate_with_a_real_seasonal_signal(
    tmp_path, make_long, make_task
):
    task, _, labels, plan, X = _setup(tmp_path, make_long, make_task, signal=True)
    rows = np.concatenate([f.test_idx for f in plan.folds])
    y_test = labels["y"].to_numpy()[rows]
    real = run_folds(labels, X, plan.folds, task, "event", with_baselines=False)
    real_score = average_precision(y_test, real.preds["logistic@sensor_like"])
    base = float(y_test.mean())
    assert real_score > base + 0.3  # the signal is learnable ...

    result = shuffled_label_check(labels, X, plan.folds, task, "event")
    assert result["ran"] and result["reference"] == pytest.approx(base)
    assert not result["suspected"]  # ... and disappears when the training labels are shuffled
    for entry in result["predictors"].values():
        assert len(entry["per_seed"]) == len(task["drills"]["shuffle_seeds"])
        assert entry["mean"] <= base + task["drills"]["shuffle_margin"]
        assert 0.3 < entry["mean_roc_auc"] < 0.7  # no skill is 0.5
    assert base < result["label_blind_expected_pr_auc"] < base + 0.1
    # a "shuffle" that does not shuffle (a broken drill, or leakage) is flagged
    assert judge_shuffle("event", real_score, base, task["drills"]["shuffle_margin"])


def test_shuffled_labels_on_pure_noise(tmp_path, make_long, make_task):
    task, _, labels, plan, X = _setup(tmp_path, make_long, make_task, signal=False, seed=5)
    result = shuffled_label_check(labels, X, plan.folds, task, "event")
    assert result["ran"] and not result["suspected"]


def test_shuffled_labels_regression(tmp_path, make_long, make_task):
    task, _, labels, plan, X = _setup(tmp_path, make_long, make_task, kind="regression")
    result = shuffled_label_check(labels, X, plan.folds, task, "regression")
    assert result["ran"] and not result["suspected"]
    assert "training-mean" in result["reference_name"]


def test_judge_shuffle_rule():
    assert judge_shuffle("event", 0.51, 0.40, 0.10) is True
    assert judge_shuffle("event", 0.50, 0.40, 0.10) is False
    assert judge_shuffle("event", float("nan"), 0.40, 0.10) is False
    assert judge_shuffle("regression", 0.80, 1.00, 0.10) is True  # 20% better than the mean
    assert judge_shuffle("regression", 0.95, 1.00, 0.10) is False
    assert judge_shuffle("regression", 1.20, 1.00, 0.10) is False


def test_shuffled_check_without_folds_says_so(tmp_path, make_long, make_task):
    task, _, labels, _, X = _setup(tmp_path, make_long, make_task)
    assert shuffled_label_check(labels, X, [], task, "event") == {
        "ran": False,
        "reason": "no walk-forward fold",
    }


def test_random_split_demo_is_labeled_not_valid(tmp_path, make_long, make_task):
    task, _, labels, plan, X = _setup(tmp_path, make_long, make_task)
    rows = np.flatnonzero((labels["date"] < plan.holdout_start).to_numpy())
    demo = invalid_random_split_demo(labels, X, rows, task, "event")
    assert demo["ran"] and demo["n"] == len(rows)
    assert demo["label"] == "what a random split would have claimed; NOT a valid estimate"
    headline = task["headline_feature_set"]
    assert set(demo["predictors"]) == {f"{a}@{headline}" for a in task["models"]["event"]}
    assert not invalid_random_split_demo(labels, X, rows[:3], task, "event")["ran"]


def test_previous_label_feature_survives_truncation_at_every_date():
    """The feature most at risk: built by hand on a tiny frame, then truncated date by date."""
    frame = pd.DataFrame(
        {
            "station": ["A", "B", "A", "A", "B"],
            "date": pd.to_datetime(
                ["2020-01-01", "2020-01-01", "2020-03-01", "2020-06-01", "2020-06-01"]
            ),
            "chlorophyll_a": [1.0, 2.0, 3.0, 4.0, 5.0],
        }
    )
    full, _ = features.previous_by_station(frame["station"], frame["date"], frame["chlorophyll_a"])
    assert np.isnan(full[0]) and np.isnan(full[1])
    assert full.tolist()[2:] == [1.0, 3.0, 2.0]
    for d in frame["date"].unique():
        past = frame[frame["date"] <= d]
        part, _ = features.previous_by_station(past["station"], past["date"], past["chlorophyll_a"])
        rows = past.index[past["date"] == d]
        assert np.array_equal(part[rows].to_numpy(), full[rows].to_numpy(), equal_nan=True)
