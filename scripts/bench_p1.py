"""Phase 1 timings: ingest 5,000 readings in one batch; fetch 100k rows resampled to 1h.

Runs the real app in-process through ``TestClient`` against a temporary SQLite file.
Usage: ``uv run python scripts/bench_p1.py``.
"""

from __future__ import annotations

import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from api.app import create_app
from api.settings import Settings

KEY = "bench-key"
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _msg(node: str, readings: list[dict]) -> dict:
    return {
        "schema_version": 1,
        "node_id": node,
        "source": "simulator",
        "sent_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "firmware": "bench",
        "readings": readings,
    }


def _readings(channel: str, unit: str, start: datetime, n: int, base: float) -> list[dict]:
    return [
        {
            "ts": (start + i * timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "channel": channel,
            "value": base + 0.01 * (i % 7),
            "unit": unit,
        }
        for i in range(n)
    ]


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(Settings(api_key=KEY, db_url=f"sqlite:///{Path(tmp) / 'bench.db'}"))
        headers = {"X-API-Key": KEY}
        with TestClient(app) as client:
            # 1) one batch of 5,000 readings (water_temp, 15-min cadence, about 52 days)
            batch = _readings("water_temp", "degC", T0, 5000, 20.0)
            t = time.perf_counter()
            r = client.post("/ingest", json=_msg("node-intake", batch), headers=headers)
            dt_ingest = time.perf_counter() - t
            assert r.status_code == 200 and r.json()["accepted"] == 5000, r.text
            print(f"ingest 5,000 readings in one batch: {dt_ingest:.3f} s")

            # 2) store 100k more rows (tds on 4 nodes x 25,000 each) in 5,000-row batches
            t = time.perf_counter()
            total = 0
            for node in ("node-intake", "node-downstream", "node-bench", "effluent-zacate"):
                for b in range(5):
                    rs = _readings("tds", "ppm", T0 + b * timedelta(days=53), 5000, 400.0)
                    rr = client.post("/ingest", json=_msg(node, rs), headers=headers).json()
                    total += rr["accepted"]
            print(f"stored {total} tds rows for the fetch test in {time.perf_counter() - t:.1f} s")
            assert total == 100_000

            t = time.perf_counter()
            r = client.get(
                "/observations", params={"channel": "tds", "resample": "1h", "agg": "mean"}
            )
            dt_fetch = time.perf_counter() - t
            body = r.json()
            assert r.status_code == 200, r.text
            print(
                f"fetch 100,000 stored rows resampled to 1h: {dt_fetch:.3f} s "
                f"({body['count']} buckets returned)"
            )
            t = time.perf_counter()
            r = client.get("/observations", params={"channel": "tds"})
            print(
                f"fetch 100,000 raw rows (no resample): {time.perf_counter() - t:.3f} s, "
                f"count={r.json()['count']}"
            )


if __name__ == "__main__":
    main()
