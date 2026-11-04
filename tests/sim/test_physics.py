"""Physics sanity: positivity, storm response, bloom presence/absence, determinism."""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np

from db.seed import load_reach
from sim.config import load_scenario, scenario_names
from sim.physics import TimeGrid, generate_world
from sim.run import main
from tests.sim.conftest import START


def _world(scenario: str, days: int = 90, seed: int = 1):
    from datetime import UTC, datetime

    params = load_scenario(scenario)
    nodes = load_reach()["nodes"]
    params["_nodes"] = nodes
    grid = TimeGrid(datetime(2026, 8, 1, tzinfo=UTC), days, 15)
    return generate_world(params, grid, seed, nodes)


def test_all_scenarios_load() -> None:
    assert set(scenario_names()) == {
        "baseline",
        "summer_bloom",
        "effluent_step",
        "storm_week",
        "sensor_faults",
    }


def test_discharge_always_positive() -> None:
    for name in scenario_names():
        w = _world(name)
        assert (w.discharge > 0).all(), name
        assert (w.discharge_h > 0).all(), name


def test_turbidity_tracks_discharge_in_storm_week() -> None:
    w = _world("storm_week")
    corr = np.corrcoef(w.discharge, w.nodes["node-intake"].turbidity)[0, 1]
    assert corr > 0.3, corr


def test_summer_bloom_exceeds_10_and_baseline_does_not() -> None:
    bloom = _world("summer_bloom")
    quiet = _world("baseline")
    assert max(n.chlorophyll_true.max() for n in bloom.nodes.values()) > 10.0
    assert max(n.chlorophyll_true.max() for n in quiet.nodes.values()) <= 10.0


def test_effluent_step_raises_downstream_phosphate() -> None:
    w = _world("effluent_step")
    down = w.nodes["node-downstream"].phosphate_po4
    before, after = down[: 25 * 96].mean(), down[35 * 96 :].mean()
    assert after > before * 1.2


def test_units_consistent_po4_vs_p(sensor_faults_30d) -> None:
    """total_phosphorus (as P) labels sit at 0.3261 * PO4 * ratio of the hidden truth."""
    truth = sensor_faults_30d.truth
    labels = [b.body for b in sensor_faults_30d.batches if b.endpoint == "/labels"]
    tps = [b for b in labels if b["channel"] == "total_phosphorus"]
    assert tps, "no total_phosphorus labels generated"
    checked = 0
    for b in tps:
        row = truth[(truth["node_id"] == b["node_id"]) & (truth["ts"] == b["ts"])]
        assert not row.empty, f"label ts {b['ts']} has no truth row"
        po4 = float(row["phosphate_po4"].iloc[0])
        assert 0.3261 * po4 * 1.25 * 0.7 < b["value"] < 0.3261 * po4 * 1.25 * 1.3
        checked += 1
    assert checked == len(tps) >= 2


def test_same_seed_same_output_different_seed_differs(tmp_path: Path) -> None:
    def run(seed: int, out: Path) -> str:
        rc = main(
            [
                "--scenario",
                "sensor_faults",
                "--days",
                "3",
                "--seed",
                str(seed),
                "--start-date",
                START,
                "--dry-run",
                "--out-dir",
                str(out),
                "--dense-labels",
            ]
        )
        assert rc == 0
        return hashlib.sha256((out / f"sensor_faults_seed{seed}.jsonl").read_bytes()).hexdigest()

    h1 = run(1, tmp_path / "a")
    h1b = run(1, tmp_path / "b")
    h2 = run(2, tmp_path / "c")
    assert h1 == h1b
    assert h1 != h2
    assert (tmp_path / "a" / "truth.csv").read_bytes() == (
        tmp_path / "b" / "truth.csv"
    ).read_bytes()
    assert (tmp_path / "a" / "faults.csv").read_bytes() == (
        tmp_path / "b" / "faults.csv"
    ).read_bytes()


def test_truth_is_never_sent(sensor_faults_30d) -> None:
    for b in sensor_faults_30d.batches:
        if b.endpoint == "/ingest":
            assert all(r["channel"] != "chlorophyll_a_true" for r in b.body["readings"])
            assert b.body["source"] == "simulator"
            assert all("emulates" in r.get("meta", {}) for r in b.body["readings"])


def test_default_start_is_days_before_now() -> None:
    from datetime import UTC, datetime, timedelta

    from sim.run import resolve_start

    start = resolve_start(None, days=5, step_min=15)
    expected = datetime.now(UTC) - timedelta(days=5)
    assert start.tzinfo is not None
    assert timedelta(0) <= expected - start < timedelta(minutes=15)
    assert start.minute % 15 == 0 and start.second == 0
    assert resolve_start("2026-08-01", 5, 15) == datetime(2026, 8, 1, tzinfo=UTC)
