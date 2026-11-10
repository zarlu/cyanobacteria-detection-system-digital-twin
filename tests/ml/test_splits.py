"""A3: chronological walk-forward by calendar year and one final hold-out."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from ml.splits import Fold, SplitError, assert_chronological, make_splits

CFG = {
    "first_test_year": 2016,
    "gap_days": 14,
    "min_train_labels": 10,
    "min_train_positives": 2,
    "final_holdout_frac": 0.20,
}


def _labels(n_years=8, per_year=6, seed=0):
    rng = np.random.default_rng(seed)
    rows = []
    for year in range(2013, 2013 + n_years):
        for k in range(per_year):
            date = pd.Timestamp(year=year, month=1 + 2 * k, day=5 if k else 3)
            for station in ("A", "B"):  # same-day replicates at two stations
                rows.append({"station": station, "date": date, "y": float(rng.random() < 0.4)})
    return pd.DataFrame(rows).sort_values(["date", "station"]).reset_index(drop=True)


def test_no_train_date_at_or_after_a_test_date_and_gap_respected():
    labels = _labels()
    plan = make_splits(labels, CFG, "event")
    assert len(plan.folds) >= 3 and plan.holdout is not None
    for fold in [*plan.folds, plan.holdout]:
        train, test = labels["date"].iloc[fold.train_idx], labels["date"].iloc[fold.test_idx]
        assert train.max() < test.min()
        assert not set(train) & set(test)
        assert train.max() < fold.boundary - pd.Timedelta(days=CFG["gap_days"])
        assert test.min() >= fold.boundary


def test_gap_removes_late_december_labels_from_training():
    labels = _labels()
    extra = pd.DataFrame({"station": ["A"], "date": [pd.Timestamp("2016-12-25")], "y": [1.0]})
    labels = pd.concat([labels, extra]).sort_values(["date", "station"]).reset_index(drop=True)
    plan = make_splits(labels, CFG, "event")
    fold_2017 = next(f for f in plan.folds if f.name == "2017")
    train_dates = labels["date"].iloc[fold_2017.train_idx]
    assert pd.Timestamp("2016-12-25") not in set(train_dates)  # inside the 14-day gap
    assert train_dates.max() < pd.Timestamp("2016-12-18")
    fold_2018 = next(f for f in plan.folds if f.name == "2018")
    assert pd.Timestamp("2016-12-25") in set(labels["date"].iloc[fold_2018.train_idx])


def test_all_rows_of_a_date_are_on_the_same_side():
    labels = _labels()
    plan = make_splits(labels, CFG, "event")
    for fold in [*plan.folds, plan.holdout]:
        test_dates = set(labels["date"].iloc[fold.test_idx])
        rows_on_test_dates = np.flatnonzero(labels["date"].isin(test_dates))
        assert set(rows_on_test_dates) == set(fold.test_idx)


def test_holdout_is_the_last_fifth_of_distinct_dates_and_never_in_a_fold():
    labels = _labels()
    plan = make_splits(labels, CFG, "event")
    distinct = np.sort(labels["date"].unique())
    n_hold = math.ceil(0.20 * len(distinct))
    assert plan.n_holdout_dates == n_hold
    assert plan.holdout_start == pd.Timestamp(distinct[-n_hold])
    hold_rows = set(plan.holdout.test_idx)
    assert hold_rows == set(np.flatnonzero(labels["date"] >= plan.holdout_start))
    for fold in plan.folds:
        assert not hold_rows & set(fold.test_idx)
        assert not hold_rows & set(fold.train_idx)
    tested = np.concatenate([f.test_idx for f in plan.folds])
    assert len(tested) == len(set(tested))  # each label is tested at most once


def test_test_fold_is_one_calendar_year():
    labels = _labels()
    plan = make_splits(labels, CFG, "event")
    for fold in plan.folds:
        years = set(labels["date"].iloc[fold.test_idx].dt.year)
        assert years == {int(fold.name)}


def test_skipped_folds_are_reported_with_the_reason():
    labels = _labels()
    few = make_splits(labels, {**CFG, "min_train_labels": 50}, "event")
    reasons = {s["fold"]: s["reason"] for s in few.skipped}
    assert "2016" in reasons and "fewer than min_train_labels=50" in reasons["2016"]
    assert all(len(f.train_idx) >= 50 for f in few.folds)

    labels_no_pos = labels.assign(y=0.0)
    none = make_splits(labels_no_pos, CFG, "event")
    assert not none.folds and none.holdout is None
    assert all("positives" in s["reason"] for s in none.skipped)
    assert "positives" in none.holdout_skipped
    # the positives rule is for event targets only
    assert make_splits(labels_no_pos, CFG, "regression").folds


def test_a_year_without_labels_is_reported():
    labels = _labels()
    labels = labels[labels["date"].dt.year != 2017].reset_index(drop=True)
    plan = make_splits(labels, CFG, "event")
    assert {"fold": "2017", "reason": "no test labels in this year"} in plan.skipped


def test_no_holdout_when_the_fraction_is_zero():
    plan = make_splits(_labels(), {**CFG, "final_holdout_frac": 0.0}, "event")
    assert plan.holdout is None and plan.holdout_start is None


def test_assert_chronological_rejects_broken_folds():
    dates = pd.Series(pd.to_datetime(["2020-01-01", "2020-06-01", "2020-12-25", "2021-03-01"]))
    boundary = pd.Timestamp("2021-01-01")
    assert_chronological(dates, Fold("ok", np.array([0, 1]), np.array([3]), boundary, 14))
    with pytest.raises(SplitError, match="at or after"):
        assert_chronological(dates, Fold("x", np.array([0, 3]), np.array([1]), boundary, 0))
    with pytest.raises(SplitError, match="both train and test"):
        assert_chronological(dates, Fold("x", np.array([0, 1]), np.array([1, 3]), boundary, 0))
    with pytest.raises(SplitError, match="gap"):
        assert_chronological(dates, Fold("x", np.array([0, 2]), np.array([3]), boundary, 14))
    with pytest.raises(SplitError, match="empty"):
        assert_chronological(dates, Fold("x", np.array([], dtype=int), np.array([3]), boundary, 0))
