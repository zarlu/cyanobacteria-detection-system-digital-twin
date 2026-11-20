"""CRP loader: contract channels only, censoring in meta, idempotent through /ingest."""

from __future__ import annotations

from connectors.crp import load
from sim.client import SimClient
from tests.conftest import TEST_API_KEY
from tests.ml.test_jobs import contract_long


def test_load_posts_contract_channels_and_is_idempotent(client, tmp_path):
    long = contract_long()
    path = tmp_path / "crp_long.csv"
    long.to_csv(path, index=False)
    in_contract = int((~long["channel"].isin(["crp_specific_conductance", "crp_tss"])).sum())

    first = load(path, SimClient("", TEST_API_KEY, http=client))
    assert first["accepted"] == in_contract and first["rejected"] == 0
    assert first["rows_outside_contract_skipped"] == len(long) - in_contract
    assert set(first["skipped_channels"]) == {"crp_specific_conductance", "crp_tss"}

    second = load(path, SimClient("", TEST_API_KEY, http=client))
    assert second["accepted"] == 0 and second["duplicates"] == in_contract

    body = client.get("/observations", params={"channel": "chlorophyll_a", "kind": "label"}).json()
    censored = [m for m in body["meta"] if m.get("censored") == "left"]
    assert censored and all(m["detection_limit"] == 3.0 for m in censored)
    assert set(body["source"]) == {"CRP"}
