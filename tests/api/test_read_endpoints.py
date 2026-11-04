"""GET /nodes, /channels, /reach."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from contract import load_registry
from tests.api.helpers import message, reading


def test_channels_match_registry(client: TestClient) -> None:
    body = client.get("/channels").json()
    registry = load_registry()
    assert {c["channel"] for c in body} == set(registry)
    ph = next(c for c in body if c["channel"] == "ph")
    assert ph["unit"] == "pH" and ph["stuck_after"] == 12 and ph["max_step_15min"] == 1.0


def test_reach_returns_config(client: TestClient) -> None:
    body = client.get("/reach").json()
    assert [n["node_id"] for n in body["nodes"]] == [
        "node-intake",
        "effluent-zacate",
        "node-downstream",
        "node-bench",
    ]
    assert body["effluent_input"] == "effluent-zacate"


def test_nodes_empty_database(client: TestClient) -> None:
    body = client.get("/nodes").json()
    assert len(body) == 4
    assert all(n["last_seen"] is None and n["good_1h"] == 0 for n in body)


def test_nodes_freshness_and_last_hour_counts(client: TestClient, auth: dict) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    rs = [
        reading(now - timedelta(minutes=30), "water_temp", 20.0, "degC"),
        reading(now - timedelta(minutes=15), "water_temp", 99.0, "degC"),  # suspect
        reading(now - timedelta(minutes=5), "ph", 7.0, "pH", quality="bad"),
        reading(now - timedelta(hours=3), "ph", 7.1, "pH"),  # outside the hour
    ]
    client.post("/ingest", json=message("node-intake", rs), headers=auth)
    node = next(n for n in client.get("/nodes").json() if n["node_id"] == "node-intake")
    assert node["good_1h"] == 1 and node["suspect_1h"] == 1 and node["bad_1h"] == 1
    assert node["n_rows"] == 4
    assert 4.5 <= node["minutes_since_seen"] <= 6.0
    assert node["last_seen"].startswith((now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M"))
