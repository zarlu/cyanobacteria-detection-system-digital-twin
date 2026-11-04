"""GET /observations: filters, resampling against a pandas reference, hard limit."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.routers import observations as obs_module
from tests.api.helpers import T0, iso, message, reading


def _synthetic_30_days(seed: int = 7) -> pd.DataFrame:
    """Irregular-ish 30-day series with quality flags set by the client (no QC triggers)."""
    rng = np.random.default_rng(seed)
    ts = pd.date_range(T0, periods=30 * 96, freq="15min", tz="UTC")
    keep = rng.random(len(ts)) > 0.1  # drop 10% to make buckets uneven
    ts = ts[keep]
    value = 20 + np.round(rng.normal(0, 0.2, len(ts)).cumsum() * 0.05, 3)
    quality = rng.choice(["good", "good", "good", "suspect", "bad"], size=len(ts))
    return pd.DataFrame({"ts": ts, "value": value, "quality": quality})


def _ingest_frame(
    client: TestClient, auth: dict, df: pd.DataFrame, node: str = "node-intake"
) -> None:
    rows = [
        reading(t.to_pydatetime(), "water_temp", float(v), "degC", quality=q)
        for t, v, q in zip(df["ts"], df["value"], df["quality"], strict=True)
    ]
    for start in range(0, len(rows), 1000):
        r = client.post("/ingest", json=message(node, rows[start : start + 1000]), headers=auth)
        assert r.status_code == 200 and r.json()["rejected"] == [], r.text


def _reference(df: pd.DataFrame, freq: str, agg: str) -> pd.DataFrame:
    rank = {"good": 0, "suspect": 1, "bad": 2}
    inv = {v: k for k, v in rank.items()}
    g = df.set_index("ts").sort_index().resample(freq)
    ref = pd.DataFrame(
        {
            "value": g["value"].agg(agg),
            "quality": g["quality"].agg(lambda s: inv[max(rank[q] for q in s)] if len(s) else None),
            "n": g["value"].size(),
        }
    )
    return ref[ref["n"] > 0]


@pytest.mark.parametrize("agg", ["mean", "median", "last", "max", "min"])
@pytest.mark.parametrize("freq", ["15min", "1h", "1D"])
def test_resample_matches_pandas_reference(
    client: TestClient, auth: dict, freq: str, agg: str
) -> None:
    df = _synthetic_30_days()
    # The series was flagged by the client; QC adds nothing because values stay in range and
    # move slowly, so the stored qualities equal the ones we sent (asserted below).
    _ingest_frame(client, auth, df)
    raw = client.get("/observations", params={"channel": "water_temp"}).json()
    assert raw["quality"] == list(df.sort_values("ts")["quality"])
    body = client.get(
        "/observations", params={"channel": "water_temp", "resample": freq, "agg": agg}
    ).json()
    ref = _reference(df, freq, agg)
    got = pd.DataFrame(
        {"value": body["value"], "quality": body["quality"], "n": body["n"]},
        index=pd.to_datetime(body["ts"], utc=True),
    )
    assert len(got) == len(ref) == body["count"]
    assert list(got.index) == list(ref.index)
    np.testing.assert_allclose(got["value"].to_numpy(), ref["value"].to_numpy(), rtol=0, atol=1e-9)
    assert got["quality"].tolist() == ref["quality"].tolist()
    assert got["n"].tolist() == ref["n"].tolist()
    assert "meta" not in body


def test_filters(client: TestClient, auth: dict) -> None:
    rs = [
        reading(T0, "water_temp", 20.0, "degC"),
        reading(T0 + timedelta(hours=1), "water_temp", 20.5, "degC", quality="suspect"),
        reading(T0 + timedelta(hours=2), "water_temp", 21.0, "degC", quality="bad"),
        reading(T0, "chlorophyll_a", 4.0, "ug/L"),
    ]
    client.post("/ingest", json=message("node-intake", rs), headers=auth)
    client.post("/ingest", json=message("node-downstream", rs[:1]), headers=auth)

    assert client.get("/observations").json()["count"] == 5
    assert client.get("/observations", params={"node_id": "node-downstream"}).json()["count"] == 1
    assert client.get("/observations", params={"kind": "label"}).json()["count"] == 1
    assert client.get("/observations", params={"min_quality": "good"}).json()["count"] == 3
    assert client.get("/observations", params={"min_quality": "suspect"}).json()["count"] == 4
    assert client.get("/observations", params={"min_quality": "bad"}).json()["count"] == 5
    window = {"start": iso(T0 + timedelta(minutes=30)), "end": iso(T0 + timedelta(hours=2))}
    assert client.get("/observations", params=window).json()["count"] == 2
    assert client.get("/observations", params={"start": "2026-10-01T00:00:00"}).status_code == 422


def test_resample_defaults_to_measurements_and_rejects_labels(
    client: TestClient, auth: dict
) -> None:
    rs = [reading(T0, "water_temp", 20.0, "degC"), reading(T0, "chlorophyll_a", 4.0, "ug/L")]
    client.post("/ingest", json=message("node-intake", rs), headers=auth)
    body = client.get("/observations", params={"resample": "1h"}).json()
    assert body["channel"] == ["water_temp"] and body["agg"] == "mean" and body["n"] == [1]
    r = client.get("/observations", params={"resample": "1h", "kind": "label"})
    assert r.status_code == 400
    assert client.get("/observations", params={"agg": "mean"}).status_code == 400


def test_resample_groups_per_series(client: TestClient, auth: dict) -> None:
    rs = [
        reading(T0, "water_temp", 20.0, "degC"),
        reading(T0 + timedelta(minutes=5), "ph", 7.0, "pH"),
    ]
    client.post("/ingest", json=message("node-intake", rs), headers=auth)
    client.post("/ingest", json=message("node-downstream", rs), headers=auth)
    body = client.get("/observations", params={"resample": "1h"}).json()
    assert body["count"] == 4
    assert sorted(zip(body["node_id"], body["channel"], strict=True)) == [
        ("node-downstream", "ph"),
        ("node-downstream", "water_temp"),
        ("node-intake", "ph"),
        ("node-intake", "water_temp"),
    ]


def test_empty_result(client: TestClient) -> None:
    body = client.get("/observations", params={"channel": "ph"}).json()
    assert body["count"] == 0 and body["ts"] == [] and body["meta"] == []
    body = client.get("/observations", params={"channel": "ph", "resample": "1D"}).json()
    assert body["count"] == 0 and body["n"] == []


def test_hard_limit_returns_413(
    client: TestClient, auth: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    rs = [reading(T0 + i * timedelta(minutes=15), "water_temp", 20.0, "degC") for i in range(11)]
    client.post("/ingest", json=message("node-intake", rs), headers=auth)
    monkeypatch.setattr(obs_module, "MAX_ROWS", 10)
    r = client.get("/observations")
    assert r.status_code == 413
    assert "resample=1h" in r.json()["detail"] and "11 rows" in r.json()["detail"]
    assert (
        client.get("/observations", params={"end": iso(T0 + timedelta(hours=2))}).status_code == 200
    )
