"""Fixtures for the ML harness tests: invented rows only (no copy of any real CRP row)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml.config import load_raw, prepare_task

STATIONS = ("90001", "90002")  # invented station ids


def _row(station, date, channel, value, quality="good", censored="none", dl=np.nan, source="CRP"):
    return {
        "ts_utc": f"{pd.Timestamp(date).date()}T18:00:00Z",
        "station": station,
        "node_id": f"crp-{station}",
        "channel": channel,
        "value": value,
        "unit": "x",
        "kind": "measurement",
        "source": source,
        "quality": quality,
        "flag_reason": "",
        "censored": censored,
        "detection_limit": dl,
    }


def build_long(
    stations=STATIONS,
    start="2011-01-12",
    end="2023-12-20",
    step_days=40,
    seed=0,
    signal=True,
    missing_share=0.15,
) -> pd.DataFrame:
    """An invented long table. With `signal`, chlorophyll follows a summer peak that water
    temperature also follows; without it, chlorophyll is noise unrelated to every feature."""
    rng = np.random.default_rng(seed)
    rows = []
    for k, station in enumerate(stations):
        date = pd.Timestamp(start) + pd.Timedelta(days=7 * k)
        while date <= pd.Timestamp(end):
            season = np.sin(2 * np.pi * (date.day_of_year - 105) / 365.25)
            if signal:
                chl = max(0.5, 5 + 16 * max(season, 0) + rng.normal(0, 2.5))
            else:
                chl = float(rng.choice([2.0, 6.0, 12.0, 20.0]))
            if chl < 3:
                rows.append(_row(station, date, "chlorophyll_a", 1.5, censored="left", dl=3.0))
            else:
                rows.append(_row(station, date, "chlorophyll_a", round(chl, 2)))
            feats = {
                "water_temp": 22 + 8 * season + rng.normal(0, 1.5),
                "ph": 8 + rng.normal(0, 0.2),
                "crp_specific_conductance": 900 + rng.normal(0, 80),
                "discharge": float(np.exp(rng.normal(3, 0.6))),
                "air_temp": 25 + 10 * season + rng.normal(0, 3),
                "total_phosphorus": float(np.exp(rng.normal(-2, 0.4))),
                "crp_tss": float(np.exp(rng.normal(3.5, 0.5))),
            }
            for channel, value in feats.items():
                if rng.random() >= missing_share:
                    rows.append(_row(station, date, channel, round(float(value), 3)))
            date += pd.Timedelta(days=step_days)
    return pd.DataFrame(rows)


def build_task(path, **overrides) -> dict:
    """The committed pooled chlorophyll spec, pointed at an invented file and made small."""
    task = load_raw("crp_chla_pooled")
    task.pop("abstract", None)
    task["name"] = "test_task"
    task["data"]["path"] = str(path)
    task["data"]["stations"] = list(STATIONS)
    task["start"] = "2011-01-01"
    task["split"]["first_test_year"] = 2015
    task["reference_probe"] = None
    task["fast"] = {
        "models": {
            "event": {"random_forest": {"n_estimators": 15}, "lightgbm": {"n_estimators": 15}},
            "regression": {"random_forest": {"n_estimators": 15}, "lightgbm": {"n_estimators": 15}},
        },
        "eval": {"n_boot": 40},
        "explain": {"n_repeats": 1},
        "drills": {"shuffle_seeds": [11, 12]},
    }
    for key, value in overrides.items():
        task[key] = value
    return prepare_task(task, fast=True)


@pytest.fixture
def make_long():
    return build_long


@pytest.fixture
def make_row():
    return _row


@pytest.fixture
def make_task():
    return build_task


@pytest.fixture
def long_csv(tmp_path):
    path = tmp_path / "long.csv"
    build_long().to_csv(path, index=False)
    return path


@pytest.fixture
def task(long_csv):
    return build_task(long_csv)
