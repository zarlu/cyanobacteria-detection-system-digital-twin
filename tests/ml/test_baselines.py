"""A4: persistence, seasonal climatology and base rate, checked against hand-computed values."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml.baselines import (
    base_rate,
    meaning,
    month_distance,
    persistence,
    predict_baseline,
    seasonal_climatology,
)


def _labels(rows):
    labels = pd.DataFrame(rows, columns=["station", "date", "y"])
    labels["date"] = pd.to_datetime(labels["date"])
    return labels


LABELS = _labels(
    [
        ("A", "2020-01-10", 0.0),  # 0 train
        ("A", "2020-05-20", 1.0),  # 1 train
        ("A", "2020-06-10", 1.0),  # 2 train
        ("A", "2020-07-10", 0.0),  # 3 train
        ("A", "2020-11-10", 0.0),  # 4 train
        ("A", "2021-06-15", 1.0),  # 5 test
        ("B", "2021-06-15", 0.0),  # 6 test: station B has no earlier label
        ("A", "2021-09-01", 0.0),  # 7 test
        ("A", "2021-12-20", 0.0),  # 8 test
    ]
)
TRAIN = np.array([0, 1, 2, 3, 4])
TEST = np.array([5, 6, 7, 8])


def test_persistence_event_is_the_previous_label_at_the_station():
    got = persistence(LABELS, TRAIN, TEST, "event")
    # row 5: previous A label is 2020-11-10 (0); row 6: no earlier label -> train rate 2/5;
    # row 7: previous A label is the TEST row 2021-06-15 (1): the latest label known at issue time;
    # row 8: previous is 2021-09-01 (0)
    assert got.tolist() == pytest.approx([0.0, 0.4, 1.0, 0.0])


def test_persistence_never_uses_the_same_date():
    labels = _labels([("A", "2020-01-01", 1.0), ("B", "2020-01-01", 0.0), ("A", "2020-02-01", 0.0)])
    got = persistence(labels, np.array([0, 1]), np.array([0, 1, 2]), "event")
    assert got.tolist() == pytest.approx([0.5, 0.5, 1.0])  # rows 0 and 1 fall back


def test_persistence_regression_falls_back_to_the_training_median():
    labels = _labels(
        [("A", "2020-01-01", 1.0), ("A", "2020-02-01", 5.0), ("A", "2020-03-01", 2.0),
         ("B", "2021-01-01", 9.0), ("A", "2021-02-01", 7.0)]
    )  # fmt: skip
    got = persistence(labels, np.array([0, 1, 2]), np.array([3, 4]), "regression")
    assert got.tolist() == pytest.approx([2.0, 2.0])  # B: median(1, 5, 2) = 2; A: previous = 2.0


def test_month_distance_wraps_around_the_year():
    assert month_distance(np.array([12, 1, 2, 3, 11]), 1).tolist() == [1, 0, 1, 2, 2]
    assert month_distance(np.array([1, 6, 12]), 12).tolist() == [1, 6, 0]


def test_seasonal_climatology_uses_the_month_window_and_its_fallback():
    got = seasonal_climatology(LABELS, TRAIN, TEST, window_months=1, min_labels=3)
    # June (rows 5, 6): train months within +/-1 are May, June, July -> mean(1, 1, 0) = 2/3
    # September: no train label in Aug to Oct -> fewer than 3 -> all-train mean 2/5
    # December: window Nov, Dec, Jan holds November (0) and January (0): 2 labels -> fallback 2/5
    assert got.tolist() == pytest.approx([2 / 3, 2 / 3, 0.4, 0.4])
    # with min_labels=2 December uses its window: mean(0, 0) = 0
    got2 = seasonal_climatology(LABELS, TRAIN, TEST, window_months=1, min_labels=2)
    assert got2.tolist() == pytest.approx([2 / 3, 2 / 3, 0.4, 0.0])
    # same month only (window 0): June has one train label -> fallback
    got0 = seasonal_climatology(LABELS, TRAIN, TEST, window_months=0, min_labels=3)
    assert got0.tolist() == pytest.approx([0.4, 0.4, 0.4, 0.4])


def test_climatology_ignores_test_labels():
    changed = LABELS.copy()
    changed.loc[TEST, "y"] = 1.0 - changed.loc[TEST, "y"]
    a = seasonal_climatology(LABELS, TRAIN, TEST)
    b = seasonal_climatology(changed, TRAIN, TEST)
    assert a.tolist() == b.tolist()


def test_base_rate_and_dispatch():
    assert base_rate(LABELS, TRAIN, TEST).tolist() == pytest.approx([0.4] * 4)
    cfg = {"climatology": {"window_months": 1, "min_labels": 3}}
    got = predict_baseline("seasonal_climatology", LABELS, TRAIN, TEST, "event", cfg)
    assert got.tolist() == pytest.approx([2 / 3, 2 / 3, 0.4, 0.4])
    with pytest.raises(ValueError, match="event targets only"):
        predict_baseline("base_rate", LABELS, TRAIN, TEST, "regression", cfg)
    with pytest.raises(ValueError, match="unknown baseline"):
        predict_baseline("nope", LABELS, TRAIN, TEST, "event", cfg)


def test_meaning_sentences_name_the_rule():
    cfg = {"climatology": {"window_months": 1, "min_labels": 3}}
    assert "the last sample at this station, carried forward" in meaning(
        "persistence", "event", cfg
    )
    assert "training base rate" in meaning("persistence", "event", cfg)
    assert "training median" in meaning("persistence", "regression", cfg)
    assert "plus or minus 1 calendar month" in meaning("seasonal_climatology", "event", cfg)
