"""Retry and backoff behavior of the simulator's HTTP client."""

from __future__ import annotations

import httpx

from sim.client import SimClient


def _client(handler) -> SimClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url="http://api")
    return SimClient(
        "http://api", "k", http=http, max_tries=4, base_backoff_s=0.01, sleep=lambda s: None
    )


def test_retries_on_5xx_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"accepted": 2, "duplicates": 0, "rejected": []})

    c = _client(handler)
    assert c.post("/ingest", {"readings": [1, 2]}) == {
        "accepted": 2,
        "duplicates": 0,
        "rejected": [],
    }
    assert calls["n"] == 3 and c.stats.accepted == 2 and c.stats.failed_requests == 0


def test_gives_up_after_max_tries() -> None:
    c = _client(lambda r: httpx.Response(500))
    assert c.post("/ingest", {}) is None
    assert c.stats.failed_requests == 1


def test_4xx_is_not_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"detail": "bad key"})

    c = _client(handler)
    assert c.post("/ingest", {}) is None
    assert calls["n"] == 1 and c.stats.failed_requests == 1


def test_transport_errors_are_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json={"accepted": 1, "duplicates": 0, "rejected": []})

    c = _client(handler)
    assert c.post("/labels", {}) is not None and calls["n"] == 2
