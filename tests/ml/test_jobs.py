"""Demo loop on invented rows: /ingest -> train (registry) -> forecast (stored predictions)."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

from connectors.crp import build_messages
from contract.registry import load_registry
from db.models import Model, Prediction
from ml.config import TaskError, load_raw, prepare_task
from ml.jobs import CLIMATOLOGY_VERSION, PERSISTENCE_VERSION, db_frame, forecast, train
from ml.source import make_labels
from ml.splits import make_splits
from tests.conftest import TEST_API_KEY
from tests.ml.conftest import STATIONS, build_long

TASK = "crp_chla_demo"


def demo_task() -> dict:
    task = load_raw(TASK)
    task["name"] = "test_demo"
    task["data"]["stations"] = list(STATIONS)
    task["start"] = "2011-01-01"
    task["split"]["first_test_year"] = 2015
    return prepare_task(task, fast=True)


def contract_long() -> pd.DataFrame:
    """The invented long table with the units and kinds the contract wants."""
    registry = load_registry()
    long = build_long()
    known = long["channel"].isin(list(registry))
    long.loc[known, "unit"] = long.loc[known, "channel"].map(lambda c: registry[c].unit)
    long.loc[long["channel"].isin(["chlorophyll_a", "total_phosphorus"]), "kind"] = "label"
    long["sample_id"] = ""
    long["meta"] = ""
    return long


@pytest.fixture
def loaded(client, session):
    messages, skipped = build_messages(contract_long())
    for message in messages:
        r = client.post("/ingest", json=message, headers={"X-API-Key": TEST_API_KEY})
        assert r.status_code == 200 and r.json()["rejected"] == []
    assert set(skipped) == {"crp_specific_conductance", "crp_tss"}
    return session


def test_train_registers_every_algorithm_with_one_champion(loaded, tmp_path):
    task = demo_task()
    rows = train(loaded, TASK, tmp_path, task=task)
    row = rows[0]
    assert row.status == "champion" and row.algorithm == task["deploy"]["algorithm"]
    assert {r.algorithm for r in rows} == set(task["models"]["event"]) | set(
        task["models"]["regression"]
    )
    assert [r.status for r in rows[1:]] == ["candidate"] * (len(rows) - 1)
    assert all((tmp_path / f"{r.version}.joblib").is_file() for r in rows)
    frame, _ = db_frame(loaded, task)
    labels, _ = make_labels(frame, task, "event")
    plan = make_splits(labels, task["split"], "event")
    gap = pd.Timedelta(days=task["split"]["gap_days"])
    assert pd.Timestamp(row.train_end.replace(tzinfo=None)) < plan.holdout_start - gap
    assert row.n_train == len(plan.holdout.train_idx)
    assert json.loads(row.features_json)["feature_set"] == "contract_channels"
    again = train(loaded, TASK, tmp_path, task=task)
    assert [r.version for r in again] == [r.version for r in rows]
    assert [r.status for r in again] == [r.status for r in rows]
    assert len(loaded.scalars(select(Model)).all()) == len(rows)


def test_forecast_stores_champion_and_persistence_once(loaded, tmp_path):
    task = demo_task()
    rows = train(loaded, TASK, tmp_path, task=task)
    row = rows[0]
    out = forecast(loaded, TASK)
    versions = {r.version for r in rows} | {PERSISTENCE_VERSION, CLIMATOLOGY_VERSION}
    assert out["visits"] > 0 and out["inserted"] == len(versions) * out["visits"]
    assert forecast(loaded, TASK)["inserted"] == 0  # idempotent
    preds = loaded.scalars(select(Prediction)).all()
    assert {p.model_version for p in preds} == versions
    train_end = row.train_end.replace(tzinfo=None)
    gap = pd.Timedelta(days=task["split"]["gap_days"])
    for p in preds:
        assert p.horizon_h == 0 and p.issued_at == p.valid_at
        assert pd.Timestamp(p.valid_at.replace(tzinfo=None)).normalize() > train_end + gap
    champion = [p for p in preds if p.model_version == row.version]
    assert all(0.0 <= p.p_event <= 1.0 and p.yhat >= 0 for p in champion)

    # persistence-v1 = the previous label at the same station, strictly earlier
    frame, _ = db_frame(loaded, task)
    labels, _ = make_labels(frame, task, "regression")
    checked = 0
    for p in preds:
        if p.model_version != PERSISTENCE_VERSION:
            continue
        station = p.node_id.removeprefix("crp-")
        day = pd.Timestamp(p.valid_at.replace(tzinfo=None)).normalize()
        earlier = labels[(labels["station"] == station) & (labels["date"] < day)]
        assert np.isclose(p.yhat, earlier.sort_values("date")["value"].iloc[-1])
        assert p.p_event == float(p.yhat >= task["target"]["event_threshold"])
        checked += 1
    assert checked == out["visits"]


def test_forecast_without_a_champion_and_train_on_an_empty_database(session, tmp_path):
    with pytest.raises(TaskError, match="no champion"):
        forecast(session, TASK)
    with pytest.raises(TaskError, match="no rows"):
        train(session, TASK, tmp_path, task=demo_task())


def test_station_climatology_predictions_match_the_harness_baseline(loaded, tmp_path):
    from ml.baselines import station_climatology

    task = demo_task()
    train(loaded, TASK, tmp_path, task=task)
    forecast(loaded, TASK)
    frame, _ = db_frame(loaded, task)
    labels, _ = make_labels(frame, task, "event")
    fold = make_splits(labels, task["split"], "event").holdout
    expected = station_climatology(labels, fold.train_idx, fold.test_idx, 1, 3)
    stored = {
        (p.node_id, pd.Timestamp(p.valid_at.replace(tzinfo=None)).normalize()): p.p_event
        for p in loaded.scalars(select(Prediction)).all()
        if p.model_version == CLIMATOLOGY_VERSION
    }
    test = labels.iloc[fold.test_idx]
    got = [stored[(f"crp-{s}", d)] for s, d in zip(test["station"], test["date"], strict=True)]
    assert np.allclose(got, expected)


def test_simulated_reading_is_labeled_not_stored_and_repeatable(loaded, tmp_path, monkeypatch):
    from datetime import date

    from db.models import Observation
    from ml import jobs
    from ml.whatif import simulate_reading

    task = demo_task()
    train(loaded, TASK, tmp_path, task=task)
    n_before = len(loaded.scalars(select(Observation)).all())
    node = f"crp-{STATIONS[0]}"
    a = simulate_reading(loaded, TASK, node, 1, date(2026, 7, 1))
    b = simulate_reading(loaded, TASK, node, 1, date(2026, 7, 1))
    c = simulate_reading(loaded, TASK, node, 2, date(2026, 7, 1))
    assert a == b and a["readings"] != c["readings"]
    assert a["source"] == "simulator" and a["stored"] is False
    assert {r["channel"] for r in a["readings"]} == {"water_temp", "ph", "discharge", "air_temp"}
    for r in a["readings"]:
        assert r["drawn_between"][0] <= r["value"] <= r["drawn_between"][1]
    assert {p["algorithm"] for p in a["predictions"]} >= {"random_forest", "station_climatology"}
    assert all(p["p_event"] is None or 0 <= p["p_event"] <= 1 for p in a["predictions"])
    assert len(loaded.scalars(select(Observation)).all()) == n_before
    assert not loaded.scalars(select(Prediction)).all()
    with pytest.raises(TaskError, match="not a station"):
        simulate_reading(loaded, TASK, "node-intake", 1, date(2026, 7, 1))
    assert a["predictions"][-1]["model_version"] == jobs.CLIMATOLOGY_VERSION
