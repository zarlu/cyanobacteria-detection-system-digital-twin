"""POST /labels and POST /labels/csv."""

from __future__ import annotations

from fastapi.testclient import TestClient

CSV_OK = """ts_utc,node_id,channel,value,unit,source,sample_id,notes
2026-10-01T12:00:00Z,node-intake,phosphate_po4,0.21,mg/L,HI713,S1,first
2026-10-02T12:00:00Z,node-intake,,0.25,,,S2,
2026-10-03T12:00:00Z,node-downstream,total_phosphorus,0.08,mg/L,CRP,S3,
"""

CSV_WITH_BAD_ROWS = """ts_utc,node_id,channel,value,unit,source,sample_id,notes
2026-10-01T12:00:00Z,node-intake,phosphate_po4,0.21,mg/L,HI713,S1,
2026-10-02T12:00:00,node-intake,phosphate_po4,0.25,mg/L,HI713,S2,naive timestamp
2026-10-03T12:00:00Z,node-intake,phosphate_po4,abc,mg/L,HI713,S3,not a number
2026-10-04T12:00:00Z,node-intake,phosphate_po4,0.30,mg/L,HI713,S4,
"""


def test_single_label_requires_key_and_defaults(client: TestClient, auth: dict) -> None:
    body = {"ts": "2026-10-01T12:00:00Z", "node_id": "node-intake", "value": 0.2, "sample_id": "S9"}
    assert client.post("/labels", json=body).status_code == 401
    r = client.post("/labels", json=body, headers=auth)
    assert r.status_code == 200 and r.json()["accepted"] == 1
    r = client.post("/labels", json=body, headers=auth)
    assert r.json()["duplicates"] == 1
    obs = client.get("/observations", params={"kind": "label"}).json()
    assert obs["channel"] == ["phosphate_po4"]
    assert obs["source"] == ["HI713"] and obs["unit"] == ["mg/L"]
    assert obs["meta"][0] == {"sample_id": "S9"}


def test_csv_upload_ok(client: TestClient, auth: dict) -> None:
    r = client.post("/labels/csv", files={"file": ("labels.csv", CSV_OK, "text/csv")}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json() == {"accepted": 3, "duplicates": 0, "rejected": []}
    obs = client.get("/observations", params={"kind": "label"}).json()
    assert sorted(obs["source"]) == ["CRP", "HI713", "HI713"]
    r = client.post("/labels/csv", files={"file": ("labels.csv", CSV_OK, "text/csv")}, headers=auth)
    assert r.json() == {"accepted": 0, "duplicates": 3, "rejected": []}


def test_csv_upload_reports_bad_rows_with_line_numbers(client: TestClient, auth: dict) -> None:
    r = client.post(
        "/labels/csv", files={"file": ("labels.csv", CSV_WITH_BAD_ROWS, "text/csv")}, headers=auth
    )
    body = r.json()
    assert body["accepted"] == 2 and body["duplicates"] == 0
    assert [x["line"] for x in body["rejected"]] == [3, 4]
    assert "naive" in body["rejected"][0]["reason"]
    assert "not a number" in body["rejected"][1]["reason"]


def test_csv_missing_columns_is_400(client: TestClient, auth: dict) -> None:
    r = client.post(
        "/labels/csv", files={"file": ("x.csv", "ts_utc,value\n1,2\n", "text/csv")}, headers=auth
    )
    assert r.status_code == 400 and "node_id" in r.json()["detail"]


def test_label_node_id_must_be_a_slug(client: TestClient, auth: dict) -> None:
    body = {"ts": "2026-10-01T12:00:00Z", "node_id": "Node Intake!", "value": 0.2}
    assert client.post("/labels", json=body, headers=auth).status_code == 422
    csv_text = (
        "ts_utc,node_id,value\n"
        "2026-10-01T12:00:00Z,ANOTHER BAD ID,0.2\n"
        "2026-10-01T12:00:00Z,node-intake,0.2\n"
    )
    r = client.post("/labels/csv", files={"file": ("x.csv", csv_text, "text/csv")}, headers=auth)
    assert r.json()["accepted"] == 1
    assert (
        r.json()["rejected"][0]["line"] == 2 and "must match" in r.json()["rejected"][0]["reason"]
    )
    assert {n["node_id"] for n in client.get("/nodes").json()} == {
        "node-intake",
        "effluent-zacate",
        "node-downstream",
        "node-bench",
    }
