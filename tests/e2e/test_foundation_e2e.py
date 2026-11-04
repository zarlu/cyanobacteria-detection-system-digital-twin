"""End-to-end: real API process + real simulator CLI + assertions over HTTP (marker ``e2e``)."""

from __future__ import annotations

import csv
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parents[2]
KEY = "e2e-key"
DAYS = 14
KEY_CHANNELS = ("water_temp", "ph", "tds", "turbidity", "discharge", "phosphate_po4", "air_temp")

pytestmark = pytest.mark.e2e


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def api(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict]:
    """Start uvicorn on a free port with a temporary database; yield url and paths."""
    tmp = tmp_path_factory.mktemp("e2e")
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    env = {**os.environ, "DB_URL": f"sqlite:///{tmp / 'e2e.db'}", "API_KEY": KEY}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api.app:create_app", "--factory", "--port", str(port)],
        cwd=REPO,
        env=env,
        stdout=(tmp / "api.log").open("w"),
        stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError("API did not start: " + (tmp / "api.log").read_text()[-2000:])
    yield {"url": url, "tmp": tmp, "out": tmp / "sim_out"}
    proc.terminate()
    proc.wait(timeout=10)


def _run_sim(api: dict, extra: list[str] | None = None) -> subprocess.CompletedProcess:
    start = (datetime.now(UTC) - timedelta(days=DAYS)).strftime("%Y-%m-%d")
    cmd = [
        sys.executable,
        "-m",
        "sim.run",
        "--scenario",
        "sensor_faults",
        "--days",
        str(DAYS),
        "--seed",
        "1",
        "--start-date",
        start,
        "--dense-labels",
        "--api-url",
        api["url"],
        "--api-key",
        KEY,
        "--out-dir",
        str(api["out"]),
        *(extra or []),
    ]
    return subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=600)


@pytest.fixture(scope="module")
def backfilled(api: dict) -> dict:
    """Run the 14-day backfill through the real CLI once for the module."""
    res = _run_sim(api)
    assert res.returncode == 0, res.stdout[-3000:] + res.stderr[-3000:]
    summary = res.stdout
    assert "rejected=0" in summary and "failed_requests=0" in summary, summary[-2000:]
    return {**api, "summary": summary}


def _get(api: dict, path: str, **params) -> dict | list:
    r = httpx.get(f"{api['url']}{path}", params=params, timeout=60)
    assert r.status_code == 200, r.text
    return r.json()


def test_every_node_reports_recent_data(backfilled: dict) -> None:
    nodes = {n["node_id"]: n for n in _get(backfilled, "/nodes")}
    assert set(nodes) >= {"node-intake", "node-downstream", "node-bench", "effluent-zacate"}
    for node_id, n in nodes.items():
        assert n["n_rows"] > 0, node_id
        assert n["last_seen"] is not None, node_id
    # sensor nodes have data up to the last step before "now" (the start date is 14 days ago)
    for node_id in ("node-intake", "node-downstream", "node-bench"):
        assert nodes[node_id]["minutes_since_seen"] < 24 * 60, (node_id, nodes[node_id])


def test_every_key_channel_has_rows(backfilled: dict) -> None:
    for channel in KEY_CHANNELS:
        body = _get(backfilled, "/observations", channel=channel, resample="1D", kind="external")
        if body["count"] == 0:
            body = _get(backfilled, "/observations", channel=channel, resample="1D")
        assert body["count"] > 0, channel


def test_resampled_and_raw_agree(backfilled: dict) -> None:
    raw = _get(backfilled, "/observations", node_id="node-intake", channel="water_temp")
    hourly = _get(
        backfilled,
        "/observations",
        node_id="node-intake",
        channel="water_temp",
        resample="1h",
        agg="mean",
    )
    assert raw["count"] > 1000 and hourly["count"] > 0
    assert sum(hourly["n"]) == raw["count"]
    daily = _get(
        backfilled,
        "/observations",
        node_id="node-intake",
        channel="water_temp",
        resample="1D",
        agg="max",
    )
    assert max(daily["value"]) <= max(raw["value"]) + 1e-9
    assert (
        abs(
            sum(v * n for v, n in zip(hourly["value"], hourly["n"], strict=True)) / sum(hourly["n"])
            - sum(raw["value"]) / len(raw["value"])
        )
        < 1e-6
    )


def test_labels_exist_and_are_simulated(backfilled: dict) -> None:
    labels = _get(backfilled, "/observations", kind="label")
    assert labels["count"] > 0
    assert set(labels["source"]) == {"simulator"}
    assert {"phosphate_po4", "chlorophyll_a"} <= set(labels["channel"])
    assert all("emulates" in m for m in labels["meta"])


def test_injected_faults_show_up_as_suspect_or_bad(backfilled: dict) -> None:
    with (backfilled["out"] / "faults.csv").open() as fh:
        faults = list(csv.DictReader(fh))
    types = {f["type"] for f in faults}
    assert {"spike", "stuck", "ph_settling", "analyzer_ref_drift"} <= types
    rows = _get(backfilled, "/observations", kind="measurement", min_quality="bad")
    flagged = [
        (n, c, t, q, m.get("reason"))
        for n, c, t, q, m in zip(
            rows["node_id"], rows["channel"], rows["ts"], rows["quality"], rows["meta"], strict=True
        )
        if q != "good"
    ]
    reasons = {r[4] for r in flagged}
    assert {"spike", "stuck", "ph_settling", "ref_drift"} <= reasons, reasons
    spikes = [f for f in faults if f["type"] == "spike"]
    caught = sum(
        1
        for f in spikes
        if any(
            r[0] == f["node_id"] and r[1] == f["channel"] and r[2] == f["start"] for r in flagged
        )
    )
    assert caught / len(spikes) >= 0.6, (caught, len(spikes))


def test_rerunning_the_backfill_adds_no_rows(backfilled: dict) -> None:
    before = _get(backfilled, "/observations")["count"]
    res = _run_sim(backfilled)
    assert res.returncode == 0, res.stderr[-2000:]
    assert "accepted=0 " in res.stdout, res.stdout[-1500:]
    assert _get(backfilled, "/observations")["count"] == before
