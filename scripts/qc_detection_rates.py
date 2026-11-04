"""Measure how much of the simulator's injected faults the server QC catches.

Runs ``sensor_faults`` through the real ingest path (in-process TestClient, temporary SQLite) and
compares stored qualities with the fault log. Used by ``tests/sim/test_backfill.py`` and
runnable on its own: ``uv run python scripts/qc_detection_rates.py [--days 30] [--seed 1]``.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from api.app import create_app
from api.settings import Settings
from sim.build import RunOutput
from sim.client import SimClient
from sim.run import generate, parse_args, post_all

KEY = "qc-key"


@dataclass
class Rates:
    """Measured detection rates (fractions in [0, 1]) and the counts behind them."""

    spikes_injected: int
    spikes_flagged_any: int
    spikes_flagged_spike: int
    stuck_readings: int
    stuck_flagged_stuck: int
    stuck_episodes: int
    stuck_episodes_detected: int
    drift_readings: int
    drift_flagged: int

    @property
    def spike_rate_any(self) -> float:
        return self.spikes_flagged_any / self.spikes_injected if self.spikes_injected else 0.0

    @property
    def spike_rate_spike(self) -> float:
        return self.spikes_flagged_spike / self.spikes_injected if self.spikes_injected else 0.0

    @property
    def stuck_reading_rate(self) -> float:
        return self.stuck_flagged_stuck / self.stuck_readings if self.stuck_readings else 0.0

    @property
    def stuck_episode_rate(self) -> float:
        return self.stuck_episodes_detected / self.stuck_episodes if self.stuck_episodes else 0.0

    @property
    def drift_rate(self) -> float:
        return self.drift_flagged / self.drift_readings if self.drift_readings else 0.0

    def table(self) -> str:
        r = self
        return "\n".join(
            [
                "QC detection on scenario sensor_faults (measured, not assumed):",
                f"  spikes: {r.spikes_injected} injected; flagged as spike "
                f"{r.spikes_flagged_spike} ({r.spike_rate_spike:.0%}); flagged suspect for any "
                f"reason {r.spikes_flagged_any} ({r.spike_rate_any:.0%})",
                f"  stuck:  {r.stuck_episodes} episodes, {r.stuck_readings} readings; readings "
                f"flagged stuck {r.stuck_flagged_stuck} ({r.stuck_reading_rate:.0%}); episodes "
                f"with >=1 flag {r.stuck_episodes_detected} ({r.stuck_episode_rate:.0%})",
                f"  turbidity drift: {r.drift_readings} drifted readings (>8 h after cleaning); "
                f"flagged by QC {r.drift_flagged} ({r.drift_rate:.0%}). QC has no drift rule, "
                "so a low number is expected and honest.",
            ]
        )


def _ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def run_and_measure(
    days: int = 30, seed: int = 1, start_date: str = "2026-08-01"
) -> tuple[Rates, RunOutput, dict]:
    """Ingest ``sensor_faults`` into a fresh app and measure detection."""
    argv = ["--scenario", "sensor_faults", "--days", str(days), "--seed", str(seed)]
    out = generate(parse_args([*argv, "--start-date", start_date]))
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(Settings(api_key=KEY, db_url=f"sqlite:///{Path(tmp) / 'qc.db'}"))
        with TestClient(app) as http:
            client = SimClient("http://testserver", KEY, http=http)
            post_all(out, client, mode="backfill", speedup=1.0, sim_start=out.batches[0].sent_at)
            stats = client.stats
            rows = http.get("/observations", params={"kind": "measurement"}).json()
    stored = {
        (n, c, _ts(t)): (q, m.get("reason"))
        for n, c, t, q, m in zip(
            rows["node_id"], rows["channel"], rows["ts"], rows["quality"], rows["meta"], strict=True
        )
    }
    good = ("good", None)
    spikes = [f for f in out.faults if f.type == "spike"]
    sp_any = sum(
        1 for f in spikes if stored.get((f.node_id, f.channel, f.start), good)[0] != "good"
    )
    sp_spike = sum(
        1 for f in spikes if stored.get((f.node_id, f.channel, f.start), good)[1] == "spike"
    )
    stuck = [f for f in out.faults if f.type == "stuck"]
    stuck_readings = stuck_flagged = episodes_detected = 0
    for f in stuck:
        inside = [
            (q, r)
            for (n, c, t), (q, r) in stored.items()
            if n == f.node_id and c == f.channel and f.start <= t <= f.end
        ]
        stuck_readings += len(inside)
        flagged = sum(1 for _, r in inside if r == "stuck")
        stuck_flagged += flagged
        episodes_detected += 1 if flagged else 0
    drift = [f for f in out.faults if f.type == "turbidity_drift"]
    drift_readings = drift_flagged = 0
    for f in drift:
        inside = [
            (q, r)
            for (n, c, t), (q, r) in stored.items()
            if n == f.node_id and c == "turbidity" and f.start + timedelta(hours=8) <= t < f.end
        ]
        drift_readings += len(inside)
        drift_flagged += sum(1 for q, _ in inside if q != "good")
    rates = Rates(
        spikes_injected=len(spikes),
        spikes_flagged_any=sp_any,
        spikes_flagged_spike=sp_spike,
        stuck_readings=stuck_readings,
        stuck_flagged_stuck=stuck_flagged,
        stuck_episodes=len(stuck),
        stuck_episodes_detected=episodes_detected,
        drift_readings=drift_readings,
        drift_flagged=drift_flagged,
    )
    totals = {
        "accepted": stats.accepted,
        "duplicates": stats.duplicates,
        "rejected": stats.rejected,
    }
    return rates, out, totals


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--seed", type=int, default=1)
    a = p.parse_args()
    rates, _, totals = run_and_measure(a.days, a.seed)
    print(f"ingest totals: {totals}")
    print(rates.table())
    return 0


if __name__ == "__main__":
    sys.exit(main())
