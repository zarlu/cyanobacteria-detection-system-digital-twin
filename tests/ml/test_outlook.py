"""Seasonal outlook: equal to the station_climatology baseline, and the window logic."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from ml.baselines import station_climatology
from ml.outlook import BASIS_OVERALL, BASIS_POOLED, BASIS_STATION, month_value, seasonal_outlook


def _labels(rows) -> pd.DataFrame:
    return pd.DataFrame(
        [{"station": s, "date": pd.Timestamp(d), "y": float(y)} for s, d, y in rows]
    )


def _history() -> pd.DataFrame:
    rows = []
    for year in range(2015, 2023):
        for month in range(1, 13):
            rows.append(("a", f"{year}-{month:02d}-10", 1 if month in (6, 7, 8) else 0))
            if month in (1, 7):
                rows.append(("b", f"{year}-{month:02d}-12", 1 if year % 2 else 0))
    return _labels(rows)


def test_month_value_equals_the_station_climatology_baseline():
    labels = _history()
    train = np.arange(len(labels))
    for station in ("a", "b"):
        for month in range(1, 13):
            probe = _labels([(station, f"2030-{month:02d}-15", 0)])
            both = pd.concat([labels, probe], ignore_index=True)
            expected = station_climatology(both, train, np.array([len(labels)]), 1, 3)[0]
            got = month_value(labels, station, month, 1, 3)
            assert got["probability"] == expected, (station, month)


def test_basis_falls_back_from_station_to_pooled_to_all():
    labels = _history()
    assert month_value(labels, "a", 7, 1, 3)["basis"] == BASIS_STATION
    assert month_value(labels, "b", 4, 1, 3)["basis"] == BASIS_POOLED  # b has no label near April
    few = _labels([("a", "2020-01-10", 1), ("a", "2020-02-10", 0)])
    assert month_value(few, "a", 7, 1, 3)["basis"] == BASIS_OVERALL
    assert month_value(few, "a", 7, 1, 3)["probability"] == 0.5


def test_next_window_is_the_first_run_of_likely_months():
    out = seasonal_outlook(_history(), "a", date(2026, 11, 12), likely_probability=0.5)
    assert len(out["months"]) == 12
    assert out["months"][0]["month_start"] == "2026-11-01" and out["months"][0]["days_until"] == 0
    window = out["next_window"]
    # June to August are events; with a +/-1 month window May..September reach 1/3, 2/3, 1, 2/3, 1/3
    assert window["month_start"] == "2027-06-01" and window["last_month_start"] == "2027-08-01"
    assert window["n_months"] == 3 and window["peak_probability"] == 1.0
    assert window["days_until"] == (date(2027, 6, 1) - date(2026, 11, 12)).days


def test_a_window_that_has_started_counts_zero_days():
    out = seasonal_outlook(_history(), "a", date(2026, 7, 20), likely_probability=0.5)
    assert out["next_window"]["days_until"] == 0
    assert out["next_window"]["last_month_start"] == "2026-08-01"


def test_no_window_and_no_labels():
    out = seasonal_outlook(_history(), "a", date(2026, 11, 12), likely_probability=1.01)
    assert out["next_window"] is None and len(out["months"]) == 12
    empty = seasonal_outlook(
        _labels([]).reindex(columns=["station", "date", "y"]), "a", date(2026, 1, 1)
    )
    assert empty["months"] == [] and empty["next_window"] is None
