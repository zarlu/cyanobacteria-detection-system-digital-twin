"""A1: good rows only, one row per (station, date), censored-label rules."""

from __future__ import annotations

import pandas as pd
import pytest

from ml.config import TaskError
from ml.source import build_frame, load_long, make_labels


def _write(tmp_path, rows):
    path = tmp_path / "rows.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def _frame(tmp_path, make_task, rows):
    path = _write(tmp_path, rows)
    task = make_task(path)
    frame, info = build_frame(load_long(path), task)
    return task, frame, info


def test_only_good_rows_of_the_configured_source_and_stations(tmp_path, make_row, make_task):
    rows = [
        make_row("90001", "2015-03-01", "chlorophyll_a", 12.0),
        make_row("90001", "2015-03-01", "water_temp", 20.0),
        make_row("90001", "2015-03-01", "ph", 9.9, quality="suspect"),
        make_row("90002", "2015-03-01", "chlorophyll_a", 40.0, quality="suspect"),
        make_row("90001", "2015-04-01", "chlorophyll_a", 99.0, source="simulator"),
        make_row("77777", "2015-03-01", "chlorophyll_a", 50.0),
    ]
    task, frame, info = _frame(tmp_path, make_task, rows)
    assert len(frame) == 1
    row = frame.iloc[0]
    assert (row["station"], row["chlorophyll_a"], row["water_temp"]) == ("90001", 12.0, 20.0)
    assert "ph" not in frame.columns or pd.isna(row["ph"])
    assert info["rows_not_good_dropped"] == 2
    labels, _ = make_labels(frame, task, "event")
    assert labels["y"].tolist() == [1.0]


def test_rows_before_start_are_dropped(tmp_path, make_row, make_task):
    rows = [
        make_row("90001", "2009-06-01", "chlorophyll_a", 30.0),
        make_row("90001", "2015-06-01", "chlorophyll_a", 30.0),
    ]
    _, frame, info = _frame(tmp_path, make_task, rows)
    assert frame["date"].dt.year.tolist() == [2015]
    assert info["rows_before_start_dropped"] == 1


def test_censored_label_rules(tmp_path, make_row, make_task):
    rows = [
        make_row("90001", "2015-01-10", "chlorophyll_a", 12.0),  # numeric, positive
        make_row("90001", "2015-02-10", "chlorophyll_a", 9.99),  # numeric, negative
        make_row("90001", "2015-03-10", "chlorophyll_a", 1.5, censored="left", dl=3.0),
        make_row("90001", "2015-04-10", "chlorophyll_a", 5.0, censored="left", dl=10.0),
        make_row("90001", "2015-05-10", "chlorophyll_a", 10.0, censored="left", dl=20.0),
        make_row("90001", "2015-06-10", "chlorophyll_a", 50.0, censored="right", dl=50.0),
        make_row("90001", "2015-07-10", "chlorophyll_a", 10.0),  # exactly at the threshold
    ]
    task, frame, info = _frame(tmp_path, make_task, rows)
    assert info["target_right_censored_dropped"] == 1

    event, e_info = make_labels(frame, task, "event")
    got = dict(zip(event["date"].dt.month, event["y"], strict=True))
    # <3 and <10 are safe negatives; <20 is unknown and dropped; >50 is dropped
    assert got == {1: 1.0, 2: 0.0, 3: 0.0, 4: 0.0, 7: 1.0}
    assert e_info["event_unknown_censored_dropped"] == 1
    assert e_info["event_censored_safe_negatives"] == 2
    assert e_info["positives"] == 2

    reg, r_info = make_labels(frame, task, "regression")
    values = dict(zip(reg["date"].dt.month, reg["value"], strict=True))
    assert values == {1: 12.0, 2: 9.99, 3: 1.5, 4: 5.0, 5: 10.0, 7: 10.0}  # detection limit / 2
    assert reg["censored"].tolist() == [False, False, True, True, True, False]
    assert r_info["labels_censored"] == 3
    assert reg["y"].iloc[0] == pytest.approx(2.5649493574615367)  # log1p(12)


def test_duplicate_station_date_channel_is_an_error(tmp_path, make_row, make_task):
    rows = [
        make_row("90001", "2015-01-10", "chlorophyll_a", 12.0),
        make_row("90001", "2015-01-10", "chlorophyll_a", 13.0),
    ]
    path = _write(tmp_path, rows)
    with pytest.raises(TaskError, match="duplicate"):
        build_frame(load_long(path), make_task(path))


def test_missing_input_file_is_an_error(tmp_path):
    with pytest.raises(TaskError, match="not found"):
        load_long(tmp_path / "nope.csv")
