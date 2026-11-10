"""Integration test on the real CRP exports. Skipped when data/raw/crp/ is empty (gitignored)."""

from __future__ import annotations

import pytest

from ml.data import crp

RAW = crp.DEFAULT_RAW_DIR
HAS_DATA = RAW.is_dir() and any(p.suffix in (".csv", ".xls", ".xlsx") for p in RAW.iterdir())
pytestmark = pytest.mark.skipif(not HAS_DATA, reason="data/raw/crp/ is empty")

# (station, file line, reason) for whole-row quarantines
QUARANTINED = {
    ("13202", 243, "date_out_of_sequence"),
    ("13202", 336, "date_out_of_sequence"),
    ("15816", 165, "date_unparseable"),
}
# (station, file line, column, reason) for suspect flags
RANGE = "out_of_range"
BLOCK = "duplicate_block"
CHL1 = "possible_detection_limit_placeholder"
CONFLICT = "conflicting_duplicate"
FLAGGED = {
    ("13202", 336, "[field block]", BLOCK),
    ("13202", 335, "[lab block]", BLOCK),
    ("13202", 327, "crp_specific_conductance", RANGE),
    ("13202", 105, "crp_specific_conductance", RANGE),
    ("13202", 328, "crp_ammonia_n", RANGE),
    ("13202", 329, "crp_tss", RANGE),
    ("13202", 2, "ph", RANGE),
    ("13202", 249, "crp_tss", RANGE),
    *{("13202", line, "chlorophyll_a", CHL1) for line in (2, 5, 6, 7, 17, 20, 26)},
    *{("13202", line, "crp_e_coli", CONFLICT) for line in (147, 153, 156)},
    ("15816", 135, "water_temp", RANGE),
    ("15816", 143, "discharge", RANGE),
    ("15816", 176, "ph", RANGE),
    ("15816", 174, "crp_total_hardness", RANGE),
    ("15816", 127, "ph", RANGE),
    ("15816", 128, "ph", RANGE),
    ("17410", 79, "[lab block]", BLOCK),
    ("17410", 80, "[field block]", BLOCK),
    ("17410", 78, "crp_secchi_depth", RANGE),
    ("17410", 10, "crp_specific_conductance", RANGE),
    ("17410", 59, "ph", RANGE),
}
# (station, file line, text) for text cells
NOTES = {
    ("13202", 27, "nd"),
    ("13202", 140, "not in swqmis"),
    ("13202", 147, "only 1 entry in swqmis"),
    ("13202", 215, "Closed By Bord.Patr."),
    ("13202", 263, "no sampling, people on site"),
    ("13202", 270, "no sampling done"),
    ("13202", 288, "Samples arrived out of temperature compliance"),
    ("13202", 315, "No entry"),
    ("13202", 330, "Coolers lost"),
    ("13202", 249, "TNTC"),
    ("13202", 210, "*12.1"),
    ("15816", 127, "No samples due to Hurricane Harvey"),
    ("15816", 123, "TNTC"),
    ("17410", 59, "Arrived at lab out of temp compliance"),
    ("17410", 60, "Arrived at lab out of temp compliance"),
    ("17410", 50, "TNTC"),
    ("17410", 41, "*44"),
}


@pytest.fixture(scope="module")
def mapping() -> crp.Mapping:
    return crp.load_mapping()


@pytest.fixture(scope="module")
def res(mapping: crp.Mapping) -> crp.Result:
    return crp.run_pipeline(RAW, mapping, crp.load_qc(mapping=mapping))


def test_every_known_issue_is_flagged_or_quarantined(res: crp.Result) -> None:
    quarantined = {(q.station, q.line, q.reason) for q in res.quarantine if not q.column}
    assert quarantined == QUARANTINED
    flagged = {(f.station, f.line, f.column, f.reason) for f in res.flags}
    assert flagged >= FLAGGED, sorted(FLAGGED - flagged)
    notes = {(n.station, n.line, n.text) for n in res.notes}
    assert notes >= NOTES, sorted(NOTES - notes)
    assert res.unresolved_order_breaks == []
    # same-date pairs: the second line's E. coli value is in quarantine, first kept as suspect
    conflicts = {(q.line, q.value_raw) for q in res.quarantine if q.reason == CONFLICT}
    assert conflicts == {(148, "3.1"), (154, "13.4"), (157, "10.8")}
    assert len(res.merges) == 3


def test_known_cell_encodings(res: crp.Result) -> None:
    files = {pf.station: {r.line: r for r in pf.rows} for pf in res.files}
    assert files["13202"][311].cells["crp_nitrate_nitrite"].kind == "left"
    assert files["13202"][311].cells["crp_nitrate_nitrite"].number == 0.05
    for line in (72, 75, 76):
        cell = files["17410"][line].cells["crp_ammonia_n"]
        assert (cell.kind, cell.number) == ("left", 0.01)
    # the footer block is not data: no row for the Average / Standard lines, #NUM! is not a note
    assert all("#NUM!" not in n.text and "6.5 - 9.0" not in n.text for n in res.notes)
    for pf in res.files:
        assert [f.cells[0] for f in pf.footers] == [
            "Average",
            "Standard",
            "Data not QAPP approved",
            "Not reportable to TCEQ",
        ]
    # quarantined rows keep their values in the quarantine file, with no guessed year
    quarantine = {(q.station, q.line): q for q in res.quarantine if not q.column}
    assert quarantine[("15816", 165)].text.startswith("04/24/204,")
    assert ",15.1," in quarantine[("15816", 165)].text
    assert ",13.1," in quarantine[("13202", 336)].text


def test_reference_counts_from_the_independent_script(res: crp.Result) -> None:
    dated = {pf.station: sum(r.date is not None for r in pf.rows) for pf in res.files}
    assert dated == {"13202": 340, "15816": 179, "17410": 84}
    chl = {}
    for pf in res.files:
        cells = [r.cells["chlorophyll_a"] for r in pf.rows if r.date is not None]
        chl[pf.station] = (
            sum(c.kind == "numeric" for c in cells),
            sum(c.kind in ("left", "right") for c in cells),
        )
    assert chl == {"13202": (54, 38), "15816": (42, 19), "17410": (42, 34)}


def test_conservation_and_traceability_on_real_files(res: crp.Result, mapping: crp.Mapping) -> None:
    records = crp.long_records(res, mapping)
    assert res.cells_parsed == len(records) + sum(q.n_cells for q in res.quarantine) + len(
        res.notes
    )
    assert all(r["value_raw"] and r["src_line"] and r["source"] == "CRP" for r in records)
    keys = [(r["node_id"], r["channel"], r["ts_utc"], r["kind"], r["source"]) for r in records]
    assert len(keys) == len(set(keys))  # the contract's uniqueness key holds
    quarantined_lines = {(q.station, q.line) for q in res.quarantine if not q.column}
    assert not {(r["station"], r["src_line"]) for r in records} & quarantined_lines
