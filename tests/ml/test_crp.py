"""CRP cleaning (ml/data/crp.py). Fixtures are invented rows that mimic the real encodings."""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from ml.data import crp

MAPPING = crp.load_mapping()
QC = crp.load_qc(mapping=MAPPING)

# Header text per channel, in file order (one of the spellings seen in real exports).
HEADERS = {
    "discharge": "Flow (ft3/sec)",
    "crp_water_depth": "Water Depth (m)",
    "crp_days_since_precip": "Days Since Last Precip",
    "air_temp": "Air Temp (deg C)",
    "water_temp": "Water Temp. (deg C)",
    "crp_dissolved_oxygen": "Dissolved Oxygen (mg/l)",
    "ph": "PH (S.U.)",
    "crp_secchi_depth": "Secchi Disc (m)",
    "crp_fecal_coliform": "Fecal Coliform (#/100ml)",
    "crp_e_coli": "E.coli (#/100)",
    "crp_specific_conductance": "Specific Conductance (S/cm)",
    "crp_chloride": "Chloride (mg/l)",
    "crp_sulfate": "Sulfate (mg/l)",
    "crp_alkalinity": "Alkalinity (mg/l)",
    "crp_total_hardness": "Total Hardness (mg/l)",
    "crp_bod": "BOD (mg/l)",
    "crp_tss": "Total Suspended Solids (mg/l)",
    "crp_vss": "Volatile Suspended Solids (mg/l)",
    "crp_tds_lab": "Total Dissolved Solids (mg/l)",
    "crp_toc": "Total Organic Carbon (mg/l)",
    "crp_ammonia_n": "Ammonia as N (mg/l)",
    "crp_nitrate_nitrite": "Nitrate + Nitrite (mg/l)",
    "crp_kjeldahl_n": "Kjeldahl-N",
    "total_phosphorus": "Total Phosphorus (mg/l)",
    "crp_ortho_phosphorus": "Ortho-Phosphorus (mg/l)",
    "chlorophyll_a": "Chlorophyll-A (ug/l)",
    "crp_pheophytin_a": "Pheophytin-a (ug/l)",
    "crp_total_calcium": "Total Calcium (mg/l)",
    "crp_total_magnesium": "Total Magnesium (mg/l)",
    "crp_total_potassium": "Total Potassium (mg/l)",
    "crp_total_sodium": "Total Sodium (mg/l)",
    "crp_total_fluoride": "Total Fluoride (mg/l)",
    "crp_total_silica": "Total Silica (mg/l)",
}
FIELD = {
    "discharge": "1234",
    "crp_water_depth": "0.7",
    "crp_days_since_precip": "> 7",
    "air_temp": "21",
    "water_temp": "19.5",
}
LAB = {
    "crp_chloride": "101",
    "crp_sulfate": "202",
    "crp_alkalinity": "121",
    "crp_total_hardness": "262",
    "crp_bod": "< 2",
    "crp_tss": "31.5",
    "crp_tds_lab": "611",
    "total_phosphorus": "0.123",
    "chlorophyll_a": "7.77",
}
FOOTER = [
    ["Average", "1500", "", "", "", "22"],
    ["Standard", "", "", "", "", "35", "5", "6.5 - 9.0", "", "#NUM!"],
    [],
    ["Data not QAPP approved"],
    ["Not reportable to TCEQ"],
]


def write_station(
    raw_dir: Path,
    station: str,
    rows: list[dict[str, str]],
    *,
    footer: bool = True,
    extra_header: list[str] | None = None,
) -> Path:
    """Write an invented export. Each row: {"date": text, <channel>: cell text, ...}."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    header = ["Date", *HEADERS.values(), *(extra_header or [])]
    table = [header]
    for row in rows:
        cells = [row.get("date", "")] + [row.get(ch, "") for ch in HEADERS]
        table.append(cells + list(row.get("_extra", [])))
    if footer:
        table += [[], *FOOTER]
    path = raw_dir / f"{station}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\r\n")
        for cells in table:
            writer.writerow(cells + [""] * (len(header) - len(cells)))
    return path


def run(raw_dir: Path, corrections: list[dict] | None = None) -> crp.Result:
    return crp.run_pipeline(raw_dir, MAPPING, QC, corrections)


def records(res: crp.Result) -> list[dict]:
    return crp.long_records(res, MAPPING)


def one(res: crp.Result, channel: str, date: str | None = None) -> dict:
    hits = [
        r
        for r in records(res)
        if r["channel"] == channel and (date is None or r["ts_utc"].startswith(date))
    ]
    assert len(hits) == 1, hits
    return hits[0]


def flags(res: crp.Result) -> set[tuple[int, str, str]]:
    return {(f.line, f.column, f.reason) for f in res.flags}


# ---------------------------------------------------------------- cells, dates, headers


@pytest.mark.parametrize(
    ("raw", "kind", "number"),
    [
        ("<3", "left", 3.0),
        ("< 3", "left", 3.0),
        ("<.05", "left", 0.05),
        ("> 2419.2", "right", 2419.2),
        (">2419.2", "right", 2419.2),
        ("\\", "missing", None),
        ("", "missing", None),
        ("   ", "missing", None),
        ("12.50", "numeric", 12.5),
        ("TNTC", "text", None),
        ("nd", "text", None),
        ("*12.1", "text", None),
        ("coolers lost", "text", None),
        ("#NUM!", "text", None),
        ("6.5 - 9.0", "text", None),
    ],
)
def test_parse_cell(raw: str, kind: str, number: float | None) -> None:
    cell = crp.parse_cell(raw)
    assert (cell.kind, cell.number) == (kind, number)
    assert cell.raw == raw


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("3/7/2011", dt.date(2011, 3, 7)),
        ("03/07/2011", dt.date(2011, 3, 7)),
        ("03/07/11", dt.date(2011, 3, 7)),
        ("11/02/98", dt.date(1998, 11, 2)),
        ("01/05/00", dt.date(2000, 1, 5)),
        ("04/24/204", None),
        ("02/30/2011", None),
        ("Average", None),
        ("", None),
    ],
)
def test_parse_date(text: str, expected: dt.date | None) -> None:
    assert crp.parse_date(text, MAPPING.two_digit_year_pivot) == expected


@pytest.mark.parametrize(
    ("header", "normalized"),
    [
        ("Water Temp (deg C)", "water temp"),
        ("Water Temp. (deg C)", "water temp"),
        ("Chlorophyll-A (ug/l)", "chlorophyll a"),
        ("E.Coli", "e coli"),
        ("E.coli (#/100)", "e coli"),
        ("E. Coli", "e coli"),
        ("Days Since Last Precip", "days since last precip"),
        ("Nitrate + Nitrite (mg/l)", "nitrate nitrite"),
    ],
)
def test_header_variants_normalize_to_the_mapping(header: str, normalized: str) -> None:
    assert crp.normalize_header(header) == normalized
    assert normalized in MAPPING.by_header


def test_unknown_column_stops_with_its_name(tmp_path: Path) -> None:
    write_station(tmp_path, "11111", [{"date": "1/2/2010"}], extra_header=["Turbidity (NTU)"])
    with pytest.raises(crp.CrpError, match="Turbidity"):
        run(tmp_path)


def test_empty_header_column_is_accepted_only_without_data(tmp_path: Path) -> None:
    write_station(tmp_path, "11111", [{"date": "1/2/2010", "ph": "8"}], extra_header=[""])
    assert len(run(tmp_path).samples) == 1
    write_station(
        tmp_path, "11111", [{"date": "1/2/2010", "ph": "8", "_extra": ["9"]}], extra_header=[""]
    )
    with pytest.raises(crp.CrpError, match="no header"):
        run(tmp_path)


@pytest.mark.parametrize("name", ["1320.csv", "station.csv", "13202.txt", "13202 copy.csv"])
def test_bad_file_name_stops(tmp_path: Path, name: str) -> None:
    write_station(tmp_path, "11111", [{"date": "1/2/2010"}]).rename(tmp_path / name)
    with pytest.raises(crp.CrpError, match="not matching"):
        run(tmp_path)


def test_empty_folder_stops(tmp_path: Path) -> None:
    with pytest.raises(crp.CrpError, match="is empty"):
        run(tmp_path)


# ---------------------------------------------------------------- rows


def test_footer_block_and_blank_lines_are_skipped(tmp_path: Path) -> None:
    write_station(tmp_path, "11111", [{"date": "1/2/2010", "ph": "8"}, {}, {"date": "2/2/2010"}])
    res = run(tmp_path)
    pf = res.files[0]
    assert [r.line for r in pf.rows] == [2, 4]
    assert [f.cells[0] for f in pf.footers] == [
        "Average",
        "Standard",
        "Data not QAPP approved",
        "Not reportable to TCEQ",
    ]
    assert len(records(res)) == 1  # only the pH value; no footer number became data
    assert res.notes == []


def test_text_cells_go_to_notes_and_never_become_numbers(tmp_path: Path) -> None:
    write_station(
        tmp_path,
        "11111",
        [
            {
                "date": "5/6/2012",
                "discharge": "gate locked, no reading",
                "crp_fecal_coliform": "TNTC",
                "crp_e_coli": "*33.3",
                "crp_vss": "nd",
                "ph": "\\",
                "water_temp": "20",
            }
        ],
    )
    res = run(tmp_path)
    assert {(n.line, n.column, n.text) for n in res.notes} == {
        (2, "discharge", "gate locked, no reading"),
        (2, "crp_fecal_coliform", "TNTC"),
        (2, "crp_e_coli", "*33.3"),
        (2, "crp_vss", "nd"),
    }
    assert [r["channel"] for r in records(res)] == ["water_temp"]


def test_unparseable_date_is_quarantined_not_dropped_not_guessed(tmp_path: Path) -> None:
    write_station(
        tmp_path,
        "11111",
        [
            {"date": "03/20/24", "chlorophyll_a": "5"},
            {"date": "04/24/204", "chlorophyll_a": "15.5", "total_phosphorus": "0.4"},
            {"date": "05/22/24", "chlorophyll_a": "6"},
        ],
    )
    res = run(tmp_path)
    assert [(q.line, q.reason, q.n_cells) for q in res.quarantine] == [(3, "date_unparseable", 2)]
    assert res.quarantine[0].text.startswith("04/24/204,")
    assert {r["ts_utc"] for r in records(res)} == {"2024-03-20T18:00:00Z", "2024-05-22T18:00:00Z"}
    assert "15.5" not in {r["value_raw"] for r in records(res)}


def test_out_of_sequence_date_is_quarantined(tmp_path: Path) -> None:
    write_station(
        tmp_path,
        "11111",
        [
            {"date": "7/31/2020", "ph": "8"},
            {"date": "11/19/2021", "ph": "8.1", "chlorophyll_a": "12"},
            {"date": "3/10/2021", "ph": "8.2"},
            {"date": "6/10/2021", "ph": "8.3"},
        ],
    )
    res = run(tmp_path)
    assert [(q.line, q.reason, q.n_cells) for q in res.quarantine] == [
        (3, "date_out_of_sequence", 2)
    ]
    assert res.unresolved_order_breaks == []
    assert [s.date.isoformat() for s in res.samples] == ["2020-07-31", "2021-03-10", "2021-06-10"]


def test_out_of_sequence_function() -> None:
    d = dt.date
    ordered = [d(2020, 1, 1), d(2020, 2, 1), d(2020, 2, 1), d(2020, 3, 1)]
    assert crp.out_of_sequence(ordered) == set()
    assert crp.out_of_sequence([d(2020, 1, 1), d(2021, 6, 1), d(2020, 3, 1)]) == {1}
    assert crp.out_of_sequence([d(2020, 5, 1), d(2019, 6, 1), d(2020, 7, 1)]) == {1}
    # first and last rows have one neighbour only: never flagged
    assert crp.out_of_sequence([d(2022, 1, 1), d(2020, 2, 1), d(2020, 3, 1)]) == set()


def test_correctly_ordered_file_flags_nothing(tmp_path: Path) -> None:
    rows = [{"date": f"{m}/15/2019", "ph": "8", "water_temp": "20"} for m in range(1, 13)]
    res = run(write_station(tmp_path, "11111", rows).parent)
    assert res.quarantine == [] and res.flags == [] and res.unresolved_order_breaks == []


def test_order_break_at_the_end_is_reported_not_quarantined(tmp_path: Path) -> None:
    write_station(
        tmp_path, "11111", [{"date": "1/5/2020", "ph": "8"}, {"date": "1/5/2019", "ph": "8"}]
    )
    res = run(tmp_path)
    assert res.quarantine == []
    assert len(res.unresolved_order_breaks) == 1


# ---------------------------------------------------------------- merge


def test_same_date_rows_merge_and_fill_blanks(tmp_path: Path) -> None:
    write_station(
        tmp_path,
        "11111",
        [
            {"date": "3/17/2009", "ph": "8", "water_temp": "20"},
            {"date": "3/17/2009", "crp_e_coli": "41", "ph": "8.0"},
        ],
    )
    res = run(tmp_path)
    assert len(res.samples) == 1 and res.samples[0].lines == [2, 3]
    assert one(res, "crp_e_coli")["src_line"] == 3
    assert one(res, "ph")["quality"] == "good"
    # the identical pH on the second row is kept once; the copy is accounted for in quarantine
    assert [(q.line, q.reason, q.column) for q in res.quarantine] == [
        (3, "duplicate_same_value", "ph")
    ]
    assert len(res.merges) == 1


def test_same_date_conflict_keeps_first_as_suspect_and_quarantines_the_other(
    tmp_path: Path,
) -> None:
    write_station(
        tmp_path,
        "11111",
        [
            {"date": "3/17/2009", "ph": "8", "crp_e_coli": "< 10"},
            {"date": "3/17/2009", "crp_e_coli": "17.5"},
        ],
    )
    res = run(tmp_path)
    kept = one(res, "crp_e_coli")
    assert (kept["value_raw"], kept["quality"], kept["flag_reason"], kept["src_line"]) == (
        "< 10",
        "suspect",
        "conflicting_duplicate",
        2,
    )
    assert [(q.line, q.reason, q.column, q.value_raw) for q in res.quarantine] == [
        (3, "conflicting_duplicate", "crp_e_coli", "17.5")
    ]
    assert (2, "crp_e_coli", "conflicting_duplicate") in flags(res)


# ---------------------------------------------------------------- screening flags


@pytest.mark.parametrize(
    ("channel", "bad", "good"),
    [
        ("water_temp", "143", "38"),
        ("water_temp", "1.5", "2"),
        ("ph", "4.65", "6.5"),
        ("ph", "9.8", "9.7"),
        ("crp_specific_conductance", "8.9", "300"),
        ("crp_specific_conductance", "3001", "3000"),
        ("discharge", "96320", "30000"),
        ("air_temp", "42.5", "42"),
        ("crp_secchi_depth", "14.7", "3"),
        ("crp_ammonia_n", "138", "20"),
        ("crp_tss", "413", "400"),
        ("crp_total_hardness", "567", "450"),
    ],
)
def test_range_flags_mark_suspect_and_keep_the_value(
    tmp_path: Path, channel: str, bad: str, good: str
) -> None:
    write_station(
        tmp_path, "11111", [{"date": "1/5/2015", channel: bad}, {"date": "2/5/2015", channel: good}]
    )
    res = run(tmp_path)
    first, second = records(res)
    assert (first["quality"], first["flag_reason"], first["value_raw"]) == (
        "suspect",
        "out_of_range",
        bad,
    )
    assert (second["quality"], second["flag_reason"]) == ("good", "")
    assert flags(res) == {(2, channel, "out_of_range")}


def test_every_range_rule_in_the_qc_file_is_covered_above() -> None:
    assert sorted(r["channel"] for r in QC["range_rules"]) == sorted(
        [
            "water_temp",
            "ph",
            "crp_specific_conductance",
            "discharge",
            "air_temp",
            "crp_secchi_depth",
            "crp_ammonia_n",
            "crp_tss",
            "crp_total_hardness",
        ]
    )


def test_chlorophyll_exactly_one_before_1999_is_flagged(tmp_path: Path) -> None:
    write_station(
        tmp_path,
        "11111",
        [
            {"date": "5/6/1996", "chlorophyll_a": "1"},
            {"date": "6/6/1996", "chlorophyll_a": "1.1"},
            {"date": "7/6/1996", "chlorophyll_a": "< 1"},
            {"date": "1/1/1999", "chlorophyll_a": "1"},
        ],
    )
    res = run(tmp_path)
    assert flags(res) == {(2, "chlorophyll_a", "possible_detection_limit_placeholder")}
    assert [r["quality"] for r in records(res)] == ["suspect", "good", "good", "good"]


def test_duplicate_field_block_flags_the_later_date(tmp_path: Path) -> None:
    write_station(
        tmp_path,
        "11111",
        [
            {"date": "11/29/2022", **FIELD, "ph": "8.1"},
            {"date": "3/3/2023", **FIELD, "water_temp": "25", "ph": "8.1"},
            {"date": "11/19/2023", **FIELD, "ph": "7.9"},
        ],
    )
    res = run(tmp_path)
    assert flags(res) == {(4, "[field block]", "duplicate_block")}
    late = [r for r in records(res) if r["ts_utc"].startswith("2023-11-19")]
    assert {r["channel"]: r["flag_reason"] for r in late} == {
        **dict.fromkeys(FIELD, "duplicate_block"),
        "ph": "",
    }
    assert all(r["quality"] == "good" for r in records(res) if r["src_line"] != 4)


def test_duplicate_lab_block_flags_the_later_date(tmp_path: Path) -> None:
    write_station(
        tmp_path,
        "11111",
        [
            {"date": "3/27/2023", **LAB},
            {"date": "7/31/2023", **LAB},
            {"date": "9/30/2023", **LAB, "chlorophyll_a": "7.78"},
        ],
    )
    res = run(tmp_path)
    assert flags(res) == {(3, "[lab block]", "duplicate_block")}
    chl = [(r["src_line"], r["quality"]) for r in records(res) if r["channel"] == "chlorophyll_a"]
    assert chl == [(2, "good"), (3, "suspect"), (4, "good")]


def test_small_identical_blocks_are_not_flagged(tmp_path: Path) -> None:
    few = {"air_temp": "21", "water_temp": "19.5", "crp_chloride": "101", "crp_sulfate": "202"}
    write_station(tmp_path, "11111", [{"date": "1/5/2015", **few}, {"date": "2/5/2015", **few}])
    assert run(tmp_path).flags == []


# ---------------------------------------------------------------- stored values


def test_flow_is_converted_to_m3s_and_keeps_the_original(tmp_path: Path) -> None:
    write_station(tmp_path, "11111", [{"date": "1/5/2015", "discharge": "1000"}])
    row = one(run(tmp_path), "discharge")
    assert float(row["value"]) == pytest.approx(28.3168)
    assert (row["unit"], row["value_raw"]) == ("m3/s", "1000")
    assert json.loads(row["meta"]) == {"date_only": True, "original_unit": "ft3/s"}


def test_left_censored_is_half_the_limit_with_raw_text_kept(tmp_path: Path) -> None:
    write_station(
        tmp_path,
        "11111",
        [{"date": "01/05/15", "chlorophyll_a": "< 3", "crp_e_coli": "> 2419.2", "ph": "8.25"}],
    )
    res = run(tmp_path)
    chl, ecoli, ph = one(res, "chlorophyll_a"), one(res, "crp_e_coli"), one(res, "ph")
    assert (chl["value"], chl["detection_limit"], chl["censored"], chl["value_raw"]) == (
        "1.5",
        "3",
        "left",
        "< 3",
    )
    assert (ecoli["value"], ecoli["detection_limit"], ecoli["censored"]) == (
        "2419.2",
        "2419.2",
        "right",
    )
    assert (ph["censored"], ph["detection_limit"]) == ("none", "")
    assert chl["ts_utc"] == "2015-01-05T18:00:00Z"
    assert (chl["kind"], ph["kind"], chl["source"]) == ("label", "measurement", "CRP")
    assert (chl["node_id"], chl["sample_id"], chl["src_file"]) == (
        "crp-11111",
        "11111-20150105",
        "11111.csv",
    )


def test_units_follow_the_contract_and_placeholders_stay_marked() -> None:
    contract = yaml.safe_load((crp.REPO / "contract" / "channels.yaml").read_text())["channels"]
    for col in MAPPING.columns:
        if col.channel in contract:
            assert col.unit == contract[col.channel]["unit"], col.channel
        else:
            assert col.channel.startswith("crp_"), col.channel
    units = {c.channel: c.unit for c in MAPPING.columns}
    assert units["crp_kjeldahl_n"] == units["crp_e_coli"] == units["crp_fecal_coliform"]
    assert units["crp_kjeldahl_n"] == "PLACEHOLDER"
    assert "phosphate_po4" not in units and "tds" not in units


# ---------------------------------------------------------------- corrections


def _corrections(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "corrections.yaml"
    path.write_text(yaml.safe_dump({"corrections": entries}), encoding="utf-8")
    return path


ENTRY = {"station": "11111", "date": "2015-01-05", "channel": "ph", "action": "confirm_ok"}


@pytest.mark.parametrize("evidence", [None, "", "   "])
def test_correction_without_evidence_is_refused(tmp_path: Path, evidence: str | None) -> None:
    entry = dict(ENTRY) if evidence is None else {**ENTRY, "evidence": evidence}
    with pytest.raises(crp.CrpError, match="evidence"):
        crp.load_corrections(_corrections(tmp_path, [entry]), MAPPING)


@pytest.mark.parametrize(
    "change",
    [
        {"action": "fix"},
        {"action": "replace"},
        {"value": 7},
        {"channel": "nope"},
        {"date": "1/5/2015"},
    ],
)
def test_malformed_correction_is_refused(tmp_path: Path, change: dict) -> None:
    entry = {**ENTRY, "evidence": "lab sheet 2015-01, checked by the owner", **change}
    with pytest.raises(crp.CrpError):
        crp.load_corrections(_corrections(tmp_path, [entry]), MAPPING)


def test_the_shipped_corrections_file_is_empty() -> None:
    assert crp.load_corrections(mapping=MAPPING) == []


def test_corrections_with_evidence_are_applied(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    write_station(
        raw, "11111", [{"date": "1/5/2015", "ph": "4.2", "water_temp": "143", "air_temp": "55"}]
    )
    ev = "field sheet scan, confirmed by the owner"
    base = {"station": "11111", "date": "2015-01-05", "evidence": ev}
    entries = [
        {**base, "channel": "ph", "action": "confirm_ok"},
        {**base, "channel": "water_temp", "action": "replace", "value": 14.3},
        {**base, "channel": "air_temp", "action": "drop"},
    ]
    res = run(raw, crp.load_corrections(_corrections(tmp_path, entries), MAPPING))
    ph, wt = one(res, "ph"), one(res, "water_temp")
    assert (ph["value"], ph["quality"], ph["flag_reason"]) == ("4.2", "good", "confirmed_ok")
    assert (wt["value"], wt["value_raw"], wt["quality"], wt["flag_reason"]) == (
        "14.3",
        "143",
        "good",
        "corrected",
    )
    assert [(q.reason, q.column, q.value_raw) for q in res.quarantine] == [
        ("dropped_by_correction", "air_temp", "55")
    ]
    assert len(res.corrections_applied) == 3
    with pytest.raises(crp.CrpError, match="no such value"):
        run(raw, [{**base, "date": dt.date(2016, 1, 1), "channel": "ph", "action": "drop"}])


# ---------------------------------------------------------------- clean outputs


def mixed_station(raw_dir: Path) -> None:
    """Two invented stations that exercise every path at once."""
    write_station(
        raw_dir,
        "11111",
        [
            {"date": "1/10/2020", **FIELD, **LAB, "ph": "8.1"},
            {"date": "2/11/2020", "chlorophyll_a": "< 3", "crp_e_coli": "> 2419.2", "ph": "\\"},
            {"date": "2/11/2020", "crp_e_coli": "55"},
            {"date": "12/17/2021", "ph": "8.3", "chlorophyll_a": "14"},
            {"date": "3/12/2020", "discharge": "no entry", "crp_fecal_coliform": "TNTC"},
            {"date": "04/24/204", "chlorophyll_a": "15.5"},
            {"date": "5/13/2020", **LAB, "water_temp": "143"},
        ],
    )
    write_station(
        raw_dir,
        "22222",
        [{"date": f"{m:02d}/15/19", "chlorophyll_a": str(4 + m), "ph": "8"} for m in range(1, 13)],
        extra_header=[""],
    )


def test_cell_conservation(tmp_path: Path) -> None:
    raw, out = tmp_path / "raw", tmp_path / "clean"
    mixed_station(raw)
    res = run(raw)
    crp.write_clean(res, MAPPING, out)

    def rows(name: str) -> list[dict[str, str]]:
        with (out / name).open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    long, quarantine, notes = (
        rows("crp_long.csv"),
        rows("crp_quarantine.csv"),
        rows("crp_notes.csv"),
    )
    assert {q["reason"] for q in quarantine} == {
        "date_unparseable",
        "date_out_of_sequence",
        "conflicting_duplicate",
    }
    assert len(notes) == 2
    assert res.cells_parsed == len(long) + sum(int(q["n_cells"]) for q in quarantine) + len(notes)
    # independent count of non-empty cells straight from the fixture files
    direct = 0
    for path in sorted(raw.glob("*.csv")):
        with path.open(newline="", encoding="utf-8") as f:
            table = list(csv.reader(f))
        end = next(i for i, r in enumerate(table) if r[0] == "Average")
        direct += sum(c.strip() not in ("", "\\") for r in table[1:end] for c in r[1:])
    assert res.cells_parsed == direct
    assert all(r["value_raw"] and r["src_line"] and r["source"] == "CRP" for r in long)
    assert {r["quality"] for r in long} <= {"good", "suspect", "bad"}
    assert {r["censored"] for r in long} == {"none", "left", "right"}
    assert list(long[0]) == crp.LONG_COLUMNS


def test_clean_twice_is_byte_identical(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    mixed_station(raw)
    digests = []
    for name in ("a", "b"):
        paths = crp.write_clean(run(raw), MAPPING, tmp_path / name)
        assert [p.name for p in paths] == [
            "crp_long.csv",
            "crp_quarantine.csv",
            "crp_notes.csv",
            "crp_profile.json",
            "crp_cleaning_report.md",
        ]
        digests.append([hashlib.sha256(p.read_bytes()).hexdigest() for p in paths])
    assert digests[0] == digests[1]


def test_no_value_is_invented_between_samples(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    write_station(
        raw,
        "11111",
        [{"date": "1/10/2020", "chlorophyll_a": "5"}, {"date": "9/10/2020", "chlorophyll_a": "25"}],
    )
    recs = records(run(raw))
    assert [(r["ts_utc"], r["value"]) for r in recs] == [
        ("2020-01-10T18:00:00Z", "5"),
        ("2020-09-10T18:00:00Z", "25"),
    ]


def test_profile_uses_good_rows_only_and_nulls_small_cells(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    rows = [
        {"date": f"7/{d}/2018", "chlorophyll_a": str(v)}
        for d, v in zip((1, 5, 9, 13, 17, 21), (4, 8, 10, 12, 20, 40), strict=True)
    ]
    rows += [
        {"date": "7/25/2018", **LAB, "chlorophyll_a": "30"},
        {"date": "7/29/2018", **LAB, "chlorophyll_a": "30"},  # duplicate lab block: suspect
        {"date": "8/2/2018", "chlorophyll_a": "< 3"},
    ]
    write_station(raw, "11111", rows)
    res = run(raw)
    profile = crp.build_profile(records(res), res, MAPPING)
    assert profile["note"].startswith("Statistical summary of real")
    assert "was not used to generate any data" in profile["note"]
    assert list(profile)[0] == "note"
    st = profile["stations"]["11111"]
    assert st["n_labels"] == 8 and st["n_labels_left_censored"] == 1  # 9 labels, 1 suspect
    assert st["labels_per_year"] == {"2018": 8}
    assert st["event_share_by_month"]["7"] == {"n": 7, "share_at_or_above": round(5 / 7, 6)}
    assert st["event_share_by_month"]["8"] == {"n": 1, "share_at_or_above": None}
    assert st["event_share_by_month"]["1"] == {"n": 0, "share_at_or_above": None}
    assert st["event_share_by_season"]["apr_sep"]["n"] == 8
    assert st["event_share_by_season"]["oct_mar"] == {"n": 0, "share_at_or_above": None}
    # label dates 1,5,9,13,17,21,25 July and 2 Aug: gaps 4,4,4,4,4,4,8
    assert st["days_between_labels"] == {"n_gaps": 7, "median": 4.0, "q1": 4.0, "q3": 4.0}
    july = st["monthly"]["chlorophyll_a"]["7"]
    assert (july["n"], july["p50"]) == (7, 12.0)
    assert st["monthly"]["chlorophyll_a"]["8"] == {"n": 1, "p10": None, "p50": None, "p90": None}
    assert st["monthly"]["discharge"]["unit"] == "m3/s"
    assert profile["stations"]["pooled"]["n_labels"] == 8


def test_percentile() -> None:
    assert crp.percentile([1, 2, 3, 4], 50) == 2.5
    assert crp.percentile([10], 90) == 10
    assert crp.percentile([0, 10], 25) == 2.5


# ---------------------------------------------------------------- manifest, verify, CLI


def test_manifest_and_verify(tmp_path: Path) -> None:
    raw, manifest = tmp_path / "raw", tmp_path / "manifest.yaml"
    path = write_station(raw, "11111", [{"date": "1/10/2020", "ph": "8"}])
    args = ["--raw-dir", str(raw), "--manifest", str(manifest)]
    assert crp.main(["verify", *args]) == 1  # no manifest yet
    assert crp.main(["manifest", *args]) == 0
    entry = yaml.safe_load(manifest.read_text())["files"][0]
    assert entry == {
        "name": "11111.csv",
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size_bytes": path.stat().st_size,
        "line_count": 8,
        "source_url": "PLACEHOLDER",
        "downloaded_on": "PLACEHOLDER",
    }
    assert crp.main(["verify", *args]) == 0

    # the owner's entries survive a rewrite
    manifest.write_text(manifest.read_text().replace("source_url: PLACEHOLDER", "source_url: x"))
    assert crp.main(["manifest", *args]) == 0
    assert yaml.safe_load(manifest.read_text())["files"][0]["source_url"] == "x"

    path.write_bytes(path.read_bytes().replace(b",8,", b",9,"))
    assert crp.main(["verify", *args]) == 1
    assert any("sha256" in p for p in crp.verify_manifest(raw, manifest, MAPPING))
    assert crp.main(["manifest", *args]) == 0
    write_station(raw, "22222", [{"date": "1/10/2020"}])
    assert crp.main(["verify", *args]) == 1


def test_cli_inspect_and_clean_on_fixtures(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    raw = tmp_path / "raw"
    mixed_station(raw)
    doc = tmp_path / "schema.md"
    assert crp.main(["inspect", "--raw-dir", str(raw), "--schema-doc", str(doc)]) == 0
    text = doc.read_text()
    assert "`Data not QAPP approved`" in text and "`Not reportable to TCEQ`" in text
    assert hashlib.sha256((raw / "11111.csv").read_bytes()).hexdigest() in text
    assert "04/24/204" in text
    assert crp.main(["clean", "--raw-dir", str(raw), "--out-dir", str(tmp_path / "out")]) == 0
    assert "date_out_of_sequence" in (tmp_path / "out" / "crp_cleaning_report.md").read_text()
    assert crp.main(["clean", "--raw-dir", str(tmp_path / "missing")]) == 2
    assert "ERROR" in capsys.readouterr().err


def test_xlsx_file_is_read_like_csv(tmp_path: Path) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["Date", *HEADERS.values()])
    row: list[object] = [dt.datetime(2020, 1, 10)] + [None] * len(HEADERS)
    row[1 + list(HEADERS).index("ph")] = 8.25
    row[1 + list(HEADERS).index("chlorophyll_a")] = "< 3"
    sheet.append(row)
    sheet.append(["Average", 1.0])
    book.save(tmp_path / "11111.xlsx")
    recs = records(run(tmp_path))
    assert [(r["ts_utc"], r["channel"], r["value"]) for r in recs] == [
        ("2020-01-10T18:00:00Z", "ph", "8.25"),
        ("2020-01-10T18:00:00Z", "chlorophyll_a", "1.5"),
    ]
