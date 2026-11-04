"""POST /ingest: auth, idempotency, per-reading validation, late and out-of-order uploads."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from db.models import Node, Observation
from tests.api.helpers import T0, message, reading, temp_series


def _count(client: TestClient) -> int:
    with client.app.state.session_factory() as s:
        return s.scalar(select(func.count()).select_from(Observation))


def test_wrong_api_key_is_401(client: TestClient) -> None:
    msg = message("node-intake", temp_series(1))
    assert client.post("/ingest", json=msg).status_code == 401
    assert client.post("/ingest", json=msg, headers={"X-API-Key": "nope"}).status_code == 401


def test_resending_a_batch_adds_zero_rows(client: TestClient, auth: dict) -> None:
    msg = message("node-intake", temp_series(10))
    r1 = client.post("/ingest", json=msg, headers=auth).json()
    assert r1 == {"accepted": 10, "duplicates": 0, "rejected": []}
    n1 = _count(client)
    r2 = client.post("/ingest", json=msg, headers=auth).json()
    assert r2 == {"accepted": 0, "duplicates": 10, "rejected": []}
    assert _count(client) == n1 == 10


def test_partially_valid_batch_stores_the_valid_part(client: TestClient, auth: dict) -> None:
    rs = temp_series(3)
    rs.insert(1, reading(T0, "water_temp", 20.0, "degF"))  # wrong unit at index 1
    rs.append({"ts": "2026-10-01T05:00:00", "channel": "water_temp", "value": 20, "unit": "degC"})
    r = client.post("/ingest", json=message("node-intake", rs), headers=auth).json()
    assert r["accepted"] == 3
    assert [x["index"] for x in r["rejected"]] == [1, 4]
    assert "must be 'degC'" in r["rejected"][0]["reason"]
    assert "naive timestamp" in r["rejected"][1]["reason"]
    assert _count(client) == 3


def test_naive_timestamp_rejected_with_clear_message(client: TestClient, auth: dict) -> None:
    rs = [{"ts": "2026-10-01T00:00:00", "channel": "ph", "value": 7.5, "unit": "pH"}]
    r = client.post("/ingest", json=message("node-intake", rs), headers=auth).json()
    assert r["accepted"] == 0
    assert r["rejected"][0]["reason"].startswith("ts: ts is a naive timestamp")
    assert "'Z' or an explicit offset" in r["rejected"][0]["reason"]


def test_duplicate_inside_batch_rejects_later_one(client: TestClient, auth: dict) -> None:
    rs = [reading(T0, "ph", 7.5, "pH"), reading(T0, "ph", 7.6, "pH")]
    r = client.post("/ingest", json=message("node-intake", rs), headers=auth).json()
    assert r["accepted"] == 1
    assert r["rejected"] == [
        {"index": 1, "reason": "duplicate_in_batch: same (channel, ts, kind, source)"}
    ]


def test_envelope_errors_are_422(client: TestClient, auth: dict) -> None:
    msg = message("node-intake", [])
    assert client.post("/ingest", json=msg, headers=auth).status_code == 422
    msg = message("Node Intake!", temp_series(1))
    assert client.post("/ingest", json=msg, headers=auth).status_code == 422


def test_out_of_order_and_week_late_bulk_upload_accepted(client: TestClient, auth: dict) -> None:
    recent = temp_series(4, start=T0 + timedelta(days=7))
    assert (
        client.post("/ingest", json=message("node-intake", recent), headers=auth).json()["accepted"]
        == 4
    )
    late = temp_series(96 * 7)  # one week of 15-min readings, all older than what is stored
    r = client.post("/ingest", json=message("node-intake", late), headers=auth).json()
    assert r["accepted"] == 96 * 7 and r["rejected"] == []
    shuffled = [late[5], late[2], late[9]]  # out of order and already stored
    r = client.post("/ingest", json=message("node-intake", shuffled), headers=auth).json()
    assert r == {"accepted": 0, "duplicates": 3, "rejected": []}
    assert _count(client) == 4 + 96 * 7


def test_unknown_node_is_auto_created_with_role_unknown(client: TestClient, auth: dict) -> None:
    r = client.post("/ingest", json=message("node-mystery", temp_series(1)), headers=auth).json()
    assert r["accepted"] == 1
    with client.app.state.session_factory() as s:
        node = s.get(Node, "node-mystery")
    assert node is not None and node.role == "unknown"


def test_reading_source_override_and_kind_default(client: TestClient, auth: dict) -> None:
    rs = [
        reading(T0, "phosphate_po4", 0.3, "mg/L", source="analyzer"),
        reading(T0, "chlorophyll_a", 5.0, "ug/L"),
    ]
    client.post("/ingest", json=message("node-downstream", rs, source="sensor"), headers=auth)
    body = client.get("/observations", params={"node_id": "node-downstream"}).json()
    by_channel = dict(
        zip(body["channel"], zip(body["kind"], body["source"], strict=True), strict=True)
    )
    assert by_channel["phosphate_po4"] == ("measurement", "analyzer")
    assert by_channel["chlorophyll_a"] == ("label", "sensor")


def test_timestamps_are_truncated_to_seconds_and_returned_utc(
    client: TestClient, auth: dict
) -> None:
    rs = [{"ts": "2026-10-01T06:30:15.987-06:00", "channel": "ph", "value": 7.0, "unit": "pH"}]
    client.post("/ingest", json=message("node-intake", rs), headers=auth)
    body = client.get("/observations", params={"channel": "ph"}).json()
    assert body["ts"] == ["2026-10-01T12:30:15Z"]
    assert body["count"] == 1
