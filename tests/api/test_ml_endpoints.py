"""ML read endpoints on an empty database and after the demo jobs."""

from __future__ import annotations

from tests.ml.test_jobs import TASK


def test_empty_database(client):
    assert client.get("/models").json() == []
    assert client.get("/predictions").json()["count"] == 0
    assert TASK in client.get("/benchmarks").json()
    assert client.get("/benchmarks/nope").status_code == 404
    assert client.get("/outlook", params={"task": "nope", "node_id": "crp-1"}).status_code == 404
    body = client.get("/outlook", params={"task": TASK, "node_id": "crp-13202"}).json()
    assert body["months"] == [] and body["next_window"] is None and body["event_threshold"] == 10


def test_benchmarks_endpoint_lists_baselines_and_models(client):
    body = client.get(f"/benchmarks/{TASK}").json()
    rows = body["kinds"]["event"]["blocks"]["walk_forward"]["rows"]
    assert any(r["key"] == "persistence" for r in rows)
    assert any(r["algorithm"] == "xgboost" for r in rows)


def test_outlook_for_a_node_outside_the_task_is_empty(client):
    body = client.get("/outlook", params={"task": TASK, "node_id": "node-intake"}).json()
    assert body["station"] is None and body["months"] == []
