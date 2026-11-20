"""A2: feature kinds, strictly-earlier previous labels, fold-local imputation."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml.config import TaskError, resolve_feature_set
from ml.features import (
    FeatureError,
    FoldImputer,
    build_features,
    complete_case_share,
    previous_by_station,
)
from ml.source import build_frame, load_long


def _frame(rows):
    frame = pd.DataFrame(rows, columns=["station", "date", "chlorophyll_a", "water_temp"])
    frame["date"] = pd.to_datetime(frame["date"])
    return frame


def test_previous_label_uses_strictly_earlier_dates_at_the_same_station():
    frame = _frame(
        [
            ("A", "2020-01-10", 4.0, 15.0),
            ("B", "2020-01-10", 30.0, 15.0),  # same date, other station: never "previous" for A
            ("A", "2020-03-10", np.nan, 18.0),  # a visit without a label
            ("A", "2020-06-10", 12.0, 28.0),
            ("B", "2020-06-10", 20.0, 28.0),
            ("A", "2020-09-10", 8.0, 27.0),
        ]
    )
    prev, days = previous_by_station(frame["station"], frame["date"], frame["chlorophyll_a"])
    assert np.isnan(prev[0]) and np.isnan(prev[1])  # first label of each station
    assert prev[2] == 4.0 and days[2] == 60  # the unlabeled visit still sees the earlier label
    assert prev[3] == 4.0 and days[3] == 152  # skips the unlabeled visit, not its own value
    assert prev[4] == 30.0 and days[4] == 152
    assert prev[5] == 12.0 and days[5] == 92
    for i in range(len(frame)):
        assert np.isnan(prev[i]) or prev[i] != frame["chlorophyll_a"][i]


def test_previous_label_feature_columns(task):
    frame = _frame(
        [
            ("90001", "2020-01-10", 4.0, 15.0),
            ("90001", "2020-06-10", 12.0, 28.0),
            ("90002", "2020-06-10", 20.0, 28.0),
        ]
    )
    X = build_features(frame, task, "sensor_like_prev")
    assert np.isnan(X["prev_label_log1p"][0]) and np.isnan(X["prev_label_log1p"][2])
    assert X["prev_label_log1p"][1] == pytest.approx(np.log1p(4.0))
    assert X["prev_label_days"][1] == 152


def test_season_station_and_transform_features(task):
    frame = _frame([("90001", "2021-04-01", 4.0, 15.0), ("90002", "2021-10-01", 4.0, 15.0)])
    frame["discharge"] = [0.0, 9.0]
    frame["crp_specific_conductance"] = [0.0, 1000.0]
    X = build_features(frame, task, "sensor_like")
    doy = pd.Timestamp("2021-04-01").day_of_year
    assert X["doy_sin"][0] == pytest.approx(np.sin(2 * np.pi * doy / 365.25))
    assert X["doy_cos"][0] == pytest.approx(np.cos(2 * np.pi * doy / 365.25))
    assert X["station_90001"].tolist() == [1.0, 0.0] and X["station_90002"].tolist() == [0.0, 1.0]
    assert X["log1p_discharge"].tolist() == pytest.approx([0.0, np.log1p(9.0)])
    assert np.isnan(X["log_specific_conductance"][0])  # log of 0 is missing, not -inf
    assert X["log_specific_conductance"][1] == pytest.approx(np.log(1000.0))
    assert X["ph"].isna().all()  # a channel with no rows is missing, not an error


def test_station_one_hot_only_when_pooled(long_csv, make_task):
    single = make_task(long_csv)
    single["data"]["stations"] = ["90001"]
    frame, _ = build_frame(load_long(long_csv), single)
    X = build_features(frame, single, "sensor_like")
    assert not [c for c in X.columns if c.startswith("station")]


def test_deployable_flag_and_set_inheritance(task):
    specs, deployable = resolve_feature_set(task, "lab_same_visit")
    names = [s["name"] for s in specs]
    assert names[:2] == ["doy_sin", "doy_cos"] and names[-1] == "log_total_phosphorus"
    assert deployable is False
    assert resolve_feature_set(task, "sensor_like_prev")[1] is True
    with pytest.raises(TaskError):
        resolve_feature_set(task, "does_not_exist")


def test_target_channel_cannot_be_a_same_visit_feature(task):
    task["feature_sets"]["bad"] = {
        "deployable": True,
        "features": [{"name": "leak", "kind": "channel", "channel": "chlorophyll_a"}],
    }
    frame = _frame([("90001", "2021-04-01", 4.0, 15.0)])
    with pytest.raises(FeatureError):
        build_features(frame, task, "bad")


def test_imputation_statistics_come_from_the_training_fold_only():
    train = pd.DataFrame({"a": [1.0, 2.0, np.nan, 3.0], "b": [10.0, 20.0, 30.0, 40.0]})
    test = pd.DataFrame({"a": [np.nan, 1000.0], "b": [np.nan, 5000.0]})
    imp = FoldImputer().fit(train, test)
    assert imp.medians_["a"] == 2.0 and imp.medians_["b"] == 25.0
    assert imp.n_fit_rows_ == len(train)
    pooled = pd.concat([train, test])
    assert imp.medians_["a"] != pooled["a"].median()  # fails if test rows enter the statistic
    assert imp.medians_["b"] != pooled["b"].median()

    out = imp.transform(test)
    assert out["a"].tolist() == [2.0, 1000.0] and out["b"].tolist() == [25.0, 5000.0]
    assert out["a__missing"].tolist() == [1.0, 0.0]
    assert out["b__missing"].tolist() == [1.0, 0.0]  # missing only in test: indicator exists
    assert imp.transform(train)["b__missing"].tolist() == [0.0] * 4

    # other test values, same training rows: identical statistics
    other = FoldImputer().fit(train, test * 7)
    assert other.medians_.equals(imp.medians_)


def test_imputer_without_missing_values_adds_no_indicator_and_handles_all_missing():
    train = pd.DataFrame({"a": [1.0, 2.0], "b": [np.nan, np.nan]})
    imp = FoldImputer().fit(train)
    assert imp.output_columns_ == ["a", "b", "b__missing"]
    assert imp.transform(train)["b"].tolist() == [0.0, 0.0]


def test_complete_case_share_overall_and_last_two_years():
    X = pd.DataFrame({"a": [1.0, np.nan, 1.0, np.nan, np.nan, 1.0]})
    dates = pd.Series(pd.to_datetime(["2020-01-01", "2021-01-01", "2022-01-01"] * 2)).sort_values()
    share = complete_case_share(X, dates, last_years=2)
    assert share["complete_share"] == pytest.approx(0.5)
    assert share["recent_years"] == [2021, 2022] and share["recent_n"] == 4
    assert share["recent_complete_share"] == pytest.approx(0.5)
    assert share["missing_share_by_feature"] == {"a": pytest.approx(0.5)}
