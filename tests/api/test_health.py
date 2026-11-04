"""GET /health."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_health(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["db_ok"] is True
    assert body["version"] == "0.1.0"
    assert body["utc_now"].endswith("Z") or "+00:00" in body["utc_now"]
