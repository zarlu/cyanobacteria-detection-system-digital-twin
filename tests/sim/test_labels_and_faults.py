"""Label noise follows the HI713 spec; every fault type appears in sensor_faults."""

from __future__ import annotations

import numpy as np

from tests.sim.conftest import run_output


def test_hi713_label_noise_matches_spec() -> None:
    out = run_output("baseline", 90, 3)
    truth = out.truth.set_index(["node_id", "ts"])
    errors, bounds = [], []
    for b in out.batches:
        if b.endpoint != "/labels" or b.body["channel"] != "phosphate_po4":
            continue
        key = (b.body["node_id"], b.body["ts"])
        if key not in truth.index:
            continue  # effluent node has no truth row (labels only) or ts between grid points
        p_true = float(truth.loc[key, "phosphate_po4"])
        bound = 0.04 + 0.04 * p_true
        errors.append(b.body["value"] - p_true)
        bounds.append(bound)
    errors, bounds = np.array(errors), np.array(bounds)
    assert len(errors) >= 40, len(errors)
    # every error inside the spec bound (plus rounding to 4 decimals); mean |error| ~ bound/2
    assert (np.abs(errors) <= bounds + 1e-4).all()
    ratio = np.abs(errors).mean() / bounds.mean()
    assert 0.35 < ratio < 0.65, ratio
    assert abs(errors.mean()) < 0.5 * bounds.mean()  # no systematic bias


def test_grab_sample_cadence_two_to_three_per_week() -> None:
    out = run_output("baseline", 70, 1)
    per_node = {}
    for b in out.batches:
        if b.endpoint == "/labels" and b.body["channel"] == "phosphate_po4":
            per_node[b.body["node_id"]] = per_node.get(b.body["node_id"], 0) + 1
    assert set(per_node) == {"node-intake", "node-downstream", "effluent-zacate"}
    for node, n in per_node.items():
        assert 2 * 10 <= n <= 3 * 10, (node, n)


def test_effluent_labels_are_censored_at_hi713_max() -> None:
    out = run_output("effluent_step", 60, 1)
    eff = [
        b.body
        for b in out.batches
        if b.endpoint == "/labels" and b.body["node_id"] == "effluent-zacate"
    ]
    late = [b for b in eff if b["ts"] >= "2026-09-01"]  # after the step on day 30
    assert late and all(b["value"] == 2.5 and b["meta"].get("censored") for b in late)


def test_dense_labels_add_weekly_chlorophyll() -> None:
    sparse = run_output("baseline", 30, 1)
    dense = run_output("baseline", 30, 1, "--dense-labels")

    def chl(o):
        return [
            b for b in o.batches if b.endpoint == "/labels" and b.body["channel"] == "chlorophyll_a"
        ]

    assert len(chl(sparse)) == 2  # one CRP sample per river node (Aug 15)
    assert len(chl(dense)) == 2 + 2 * 4  # plus 4 Mondays x 2 nodes


def test_every_fault_type_appears_in_sensor_faults(sensor_faults_30d) -> None:
    types = {f.type for f in sensor_faults_30d.faults}
    assert types == {
        "turbidity_drift",
        "ph_settling",
        "wifi_outage",
        "stuck",
        "spike",
        "analyzer_ref_drift",
        "node_silent",
    }
    for f in sensor_faults_30d.faults:
        assert f.end >= f.start


def test_baseline_has_no_faults() -> None:
    assert run_output("baseline", 10, 1).faults == []


def test_ph_settling_flags_come_from_the_node(sensor_faults_30d) -> None:
    flagged = [
        r
        for b in sensor_faults_30d.batches
        if b.endpoint == "/ingest"
        for r in b.body["readings"]
        if r["channel"] == "ph" and r.get("quality") == "suspect"
    ]
    assert flagged and all(r["meta"]["reason"] == "ph_settling" for r in flagged)


def test_analyzer_ref_drift_marks_cycles_bad(sensor_faults_30d) -> None:
    bad = [
        r
        for b in sensor_faults_30d.batches
        if b.endpoint == "/ingest"
        for r in b.body["readings"]
        if r["meta"].get("emulates") == "analyzer" and r.get("quality") == "bad"
    ]
    assert bad and all(r["meta"]["reason"] == "ref_drift" for r in bad)


def test_outage_batch_is_late_and_contains_duplicates(sensor_faults_30d) -> None:
    outages = [f for f in sensor_faults_30d.faults if f.type == "wifi_outage"]
    assert len(outages) == 2
    for f in outages:
        late = [
            b
            for b in sensor_faults_30d.batches
            if b.endpoint == "/ingest"
            and b.node_id == f.node_id
            and b.sent_at > f.end
            and b.sent_at <= f.end.replace(second=0) + __import__("datetime").timedelta(minutes=2)
        ]
        assert late, "no late bulk upload after the outage"
        ts = [r["ts"] for r in late[0].body["readings"]]
        assert min(ts) < f.start.strftime("%Y-%m-%dT%H:%M:%SZ")  # includes re-sent older readings
