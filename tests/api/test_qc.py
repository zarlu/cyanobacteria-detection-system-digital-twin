"""Server-side QC: ranges, saturation, spikes, stuck values; downgrade only."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.api.helpers import STEP, T0, message, reading


def _post(client: TestClient, auth: dict, node: str, rs: list[dict]) -> dict:
    r = client.post("/ingest", json=message(node, rs), headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def _rows(client: TestClient, **params: str) -> dict:
    return client.get("/observations", params=params).json()


def test_out_of_range_downgrades_to_suspect_and_keeps_value(client: TestClient, auth: dict) -> None:
    _post(client, auth, "node-intake", [reading(T0, "water_temp", 60.0, "degC")])
    body = _rows(client, channel="water_temp")
    assert body["value"] == [60.0]
    assert body["quality"] == ["suspect"]
    assert body["meta"][0]["reason"] == "out_of_range"


def test_tds_saturation(client: TestClient, auth: dict) -> None:
    _post(client, auth, "node-intake", [reading(T0, "tds", 960.0, "ppm")])
    body = _rows(client, channel="tds")
    assert body["quality"] == ["suspect"] and body["meta"][0]["reason"] == "saturated"


def test_spike_is_flagged_once_and_recovery_is_not(client: TestClient, auth: dict) -> None:
    rs = [
        reading(T0, "water_temp", 20.0, "degC"),
        reading(T0 + STEP, "water_temp", 20.1, "degC"),
        reading(T0 + 2 * STEP, "water_temp", 29.0, "degC"),  # +8.9 in 15 min, limit 3
        reading(T0 + 3 * STEP, "water_temp", 20.2, "degC"),  # back to normal vs last good
    ]
    _post(client, auth, "node-intake", rs)
    body = _rows(client, channel="water_temp")
    assert body["quality"] == ["good", "good", "suspect", "good"]
    assert body["meta"][2]["reason"] == "spike"


def test_spike_threshold_scales_with_gap(client: TestClient, auth: dict) -> None:
    rs = [
        reading(T0, "water_temp", 20.0, "degC"),
        reading(T0 + 4 * STEP, "water_temp", 28.0, "degC"),
    ]
    _post(client, auth, "node-intake", rs)  # +8 over 60 min; allowed 3 * 4 = 12
    assert _rows(client, channel="water_temp")["quality"] == ["good", "good"]


def test_spike_uses_stored_history_across_batches(client: TestClient, auth: dict) -> None:
    _post(client, auth, "node-intake", [reading(T0, "water_temp", 20.0, "degC")])
    _post(client, auth, "node-intake", [reading(T0 + STEP, "water_temp", 30.0, "degC")])
    assert _rows(client, channel="water_temp")["quality"] == ["good", "suspect"]


def test_stuck_after_twelve_identical_values(client: TestClient, auth: dict) -> None:
    rs = [reading(T0 + i * STEP, "ph", 7.4, "pH") for i in range(15)]
    _post(client, auth, "node-intake", rs)
    body = _rows(client, channel="ph")
    assert body["quality"] == ["good"] * 11 + ["suspect"] * 4
    assert body["meta"][11]["reason"] == "stuck"
    assert body["meta"][10] == {}


def test_no_stuck_check_on_channels_without_stuck_after(client: TestClient, auth: dict) -> None:
    rs = [reading(T0 + i * STEP, "flow_rate", 0.0, "L/min") for i in range(20)]
    _post(client, auth, "node-bench", rs)
    assert set(_rows(client, channel="flow_rate")["quality"]) == {"good"}


def test_qc_never_upgrades_quality(client: TestClient, auth: dict) -> None:
    rs = [
        reading(T0, "water_temp", 20.0, "degC", quality="suspect", meta={"reason": "sensor_error"}),
        reading(T0 + STEP, "water_temp", 20.0, "degC", quality="bad"),
        reading(T0 + 2 * STEP, "water_temp", 99.0, "degC", quality="bad"),  # out of range too
        reading(T0 + 3 * STEP, "ph", 7.0, "pH", quality="suspect", meta={"reason": "ph_settling"}),
    ]
    _post(client, auth, "node-intake", rs)
    body = _rows(client, node_id="node-intake")
    by_ts = dict(zip(body["ts"], zip(body["quality"], body["meta"], strict=True), strict=True))
    assert by_ts["2026-10-01T00:00:00Z"][0] == "suspect"
    assert by_ts["2026-10-01T00:00:00Z"][1]["reason"] == "sensor_error"
    assert by_ts["2026-10-01T00:15:00Z"][0] == "bad"
    assert by_ts["2026-10-01T00:30:00Z"][0] == "bad"
    assert by_ts["2026-10-01T00:30:00Z"][1]["reason"] == "out_of_range"
    assert by_ts["2026-10-01T00:45:00Z"] == ("suspect", {"reason": "ph_settling"})


def test_node_reason_kept_and_qc_reason_added(client: TestClient, auth: dict) -> None:
    rs = [reading(T0, "ph", 3.0, "pH", quality="suspect", meta={"reason": "ph_settling"})]
    _post(client, auth, "node-intake", rs)
    meta = _rows(client, channel="ph")["meta"][0]
    assert meta == {"reason": "ph_settling", "qc_reason": "out_of_range"}


def test_labels_get_range_check_but_no_spike_or_stuck(client: TestClient, auth: dict) -> None:
    rs = [reading(T0 + i * STEP, "chlorophyll_a", 5.0, "ug/L", kind="label") for i in range(14)]
    rs.append(reading(T0 + 14 * STEP, "chlorophyll_a", 900.0, "ug/L", kind="label"))
    _post(client, auth, "node-intake", rs)
    body = _rows(client, channel="chlorophyll_a")
    assert body["quality"] == ["good"] * 14 + ["suspect"]
