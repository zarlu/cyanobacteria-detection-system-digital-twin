"""Backfill through the real API: QC detection floors, 90-day completion, outage idempotency."""

from __future__ import annotations

from fastapi.testclient import TestClient

from scripts.qc_detection_rates import run_and_measure
from sim.client import SimClient
from sim.run import post_all
from tests.sim.conftest import run_output

# The run below is fully deterministic (seed 1, fixed start date), measured on 2026-11-03:
# spikes flagged for any reason 86% (42/49), flagged as `spike` 39% (19/49); stuck readings 45%
# (65/144), stuck episodes 56% (5/9). Floors sit a few points under those values so the test fails
# when QC regresses (a rule disabled or weakened) yet tolerates small changes to the simulator's
# noise streams. If you change the physics or the fault parameters, re-measure with
# `uv run python scripts/qc_detection_rates.py --days 30` and update these numbers and ASSUMPTIONS.
SPIKE_ANY_FLOOR = 0.80
SPIKE_SPIKE_FLOOR = 0.35
STUCK_READING_FLOOR = 0.40
STUCK_EPISODE_FLOOR = 0.50


def test_qc_catches_injected_spikes_and_stuck_periods() -> None:
    rates, _, totals = run_and_measure(days=30, seed=1)
    print("\n" + rates.table())
    assert totals["rejected"] == 0
    assert totals["duplicates"] > 0  # the outage replays re-send readings
    assert rates.spikes_injected >= 20
    assert rates.spike_rate_any >= SPIKE_ANY_FLOOR, rates.table()
    assert rates.spike_rate_spike >= SPIKE_SPIKE_FLOOR, rates.table()
    assert rates.stuck_reading_rate >= STUCK_READING_FLOOR, rates.table()
    assert rates.stuck_episode_rate >= STUCK_EPISODE_FLOOR, rates.table()


def test_90_day_backfill_completes_and_is_readable(client: TestClient, auth: dict) -> None:
    # 60-minute step keeps this under the unit-test budget; the 15-min version runs in e2e.
    out = run_output("summer_bloom", 90, 1, "--step-min", "60", "--dense-labels")
    sim = SimClient("http://testserver", auth["X-API-Key"], http=client)
    post_all(out, sim, mode="backfill", speedup=1.0, sim_start=out.batches[0].sent_at)
    assert sim.stats.failed_requests == 0 and sim.stats.rejected == 0
    assert sim.stats.accepted == sum(out.readings_per_node_channel.values())
    nodes = {n["node_id"]: n for n in client.get("/nodes").json()}
    for node_id in ("node-intake", "node-downstream", "node-bench", "effluent-zacate"):
        assert nodes[node_id]["n_rows"] > 0, node_id
    for channel in ("water_temp", "ph", "tds", "turbidity", "discharge", "phosphate_po4"):
        body = client.get(
            "/observations",
            params={"channel": channel, "resample": "1D", "kind": "measurement"}
            if channel != "discharge"
            else {"channel": channel, "resample": "1D", "kind": "external"},
        ).json()
        assert body["count"] > 0, channel
    labels = client.get("/observations", params={"kind": "label"}).json()
    assert set(labels["channel"]) == {"phosphate_po4", "chlorophyll_a", "total_phosphorus"}
    assert set(labels["source"]) == {"simulator"}
    chl_labels = [
        v for v, c in zip(labels["value"], labels["channel"], strict=True) if c == "chlorophyll_a"
    ]
    assert out.truth["chlorophyll_a_true"].max() > 10 and max(chl_labels) > 5


def test_outage_replay_creates_no_duplicate_rows(client: TestClient, auth: dict) -> None:
    out = run_output("sensor_faults", 12, 1, "--step-min", "30")
    sim = SimClient("http://testserver", auth["X-API-Key"], http=client)
    post_all(out, sim, mode="backfill", speedup=1.0, sim_start=out.batches[0].sent_at)
    n_unique = sum(out.readings_per_node_channel.values())
    assert sim.stats.duplicates > 0
    assert sim.stats.accepted == n_unique
    first = client.get("/observations").json()["count"]
    assert first == n_unique
    # re-run the whole backfill: nothing changes
    sim2 = SimClient("http://testserver", auth["X-API-Key"], http=client)
    post_all(out, sim2, mode="backfill", speedup=1.0, sim_start=out.batches[0].sent_at)
    assert sim2.stats.accepted == 0
    assert client.get("/observations").json()["count"] == first
