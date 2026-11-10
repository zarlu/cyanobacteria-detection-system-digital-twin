"""IBWC Clean Rivers Program (CRP) exports: inspect, clean, manifest, verify.

    python -m ml.data.crp inspect|clean|manifest|verify

Real grab-sample data, one file per station in ``data/raw/crp/``. This module reads the files
as they are, never edits them, never interpolates and never turns text into numbers. Every
station, column, unit and threshold comes from the YAML files next to this module.

Pipeline: read file -> classify rows (data, blank, footer) -> parse cells -> quarantine rows with
an unusable date -> merge rows sharing (station, date) -> screening flags (suspect, value kept).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
DEFAULT_RAW_DIR = REPO / "data" / "raw" / "crp"
DEFAULT_MAPPING = HERE / "crp_mapping.yaml"
DEFAULT_QC = HERE / "crp_qc.yaml"
DEFAULT_CORRECTIONS = HERE / "crp_corrections.yaml"
DEFAULT_SCHEMA_DOC = REPO / "docs" / "crp_schema.md"
DEFAULT_OUT_DIR = REPO / "data" / "clean"
DEFAULT_MANIFEST = HERE / "crp_manifest.yaml"

CORRECTION_ACTIONS = ("replace", "confirm_ok", "drop")

_NUM = r"[+-]?(?:\d+\.?\d*|\.\d+)"
RE_NUMBER = re.compile(rf"^{_NUM}$")
RE_CENSORED = re.compile(rf"^([<>])\s*({_NUM})$")
RE_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})$")


class CrpError(Exception):
    """A problem that must stop the run (bad file name, unknown column, bad config)."""


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Column:
    header: str
    channel: str
    unit: str
    kind: str
    source_unit: str | None = None
    factor: float = 1.0
    note: str = ""


@dataclass(frozen=True)
class Mapping:
    source: str
    file_pattern: re.Pattern[str]
    node_id_template: str
    date_header: str
    two_digit_year_pivot: int
    stored_time_utc: str
    date_meta: dict[str, Any]
    missing_markers: frozenset[str]
    summary_rows: tuple[str, ...]
    disclaimer_rows: tuple[str, ...]
    stations: dict[str, dict[str, Any]]
    columns: tuple[Column, ...]
    profile: dict[str, Any]

    @property
    def by_header(self) -> dict[str, Column]:
        return {c.header: c for c in self.columns}

    @property
    def channels(self) -> list[str]:
        return [c.channel for c in self.columns]

    def column(self, channel: str) -> Column:
        for c in self.columns:
            if c.channel == channel:
                return c
        raise CrpError(f"channel {channel!r} is not in the mapping")


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise CrpError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CrpError(f"{path}: expected a YAML mapping at the top level")
    return data


def load_mapping(path: Path = DEFAULT_MAPPING) -> Mapping:
    raw = _read_yaml(path)
    try:
        columns = tuple(
            Column(
                header=str(c["header"]),
                channel=str(c["channel"]),
                unit=str(c["unit"]),
                kind=str(c["kind"]),
                source_unit=c.get("source_unit"),
                factor=float(c.get("factor", 1.0)),
                note=str(c.get("note", "")),
            )
            for c in raw["columns"]
        )
        mapping = Mapping(
            source=str(raw["source"]),
            file_pattern=re.compile(str(raw["file_pattern"])),
            node_id_template=str(raw["node_id_template"]),
            date_header=str(raw["date"]["header"]),
            two_digit_year_pivot=int(raw["date"]["two_digit_year_pivot"]),
            stored_time_utc=str(raw["date"]["stored_time_utc"]),
            date_meta=dict(raw["date"].get("meta", {})),
            missing_markers=frozenset(str(m) for m in raw["missing_markers"]),
            summary_rows=tuple(raw["footer"]["summary_rows"]),
            disclaimer_rows=tuple(raw["footer"]["disclaimer_rows"]),
            stations={str(k): dict(v) for k, v in raw["stations"].items()},
            columns=columns,
            profile=dict(raw.get("profile") or {}),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CrpError(f"{path}: invalid mapping ({exc!r})") from exc
    for name, values in (("header", [c.header for c in columns]), ("channel", mapping.channels)):
        dupes = sorted(v for v, n in Counter(values).items() if n > 1)
        if dupes:
            raise CrpError(f"{path}: duplicate {name} in columns: {dupes}")
    bad_kinds = sorted({c.kind for c in columns} - {"label", "measurement", "external"})
    if bad_kinds:
        raise CrpError(f"{path}: unknown kind(s) {bad_kinds}")
    return mapping


def load_qc(path: Path = DEFAULT_QC, mapping: Mapping | None = None) -> dict[str, Any]:
    qc = _read_yaml(path)
    for key in ("range_rules", "equals_before_rules", "duplicate_blocks"):
        qc.setdefault(key, [])
    qc.setdefault("row_quarantine", {})
    qc.setdefault("same_date_merge", {"conflict_reason": "conflicting_duplicate"})
    for rule in qc["equals_before_rules"]:
        rule["before"] = dt.date.fromisoformat(str(rule["before"]))
    if mapping is not None:
        known = set(mapping.channels)
        used = [r["channel"] for r in qc["range_rules"] + qc["equals_before_rules"]]
        used += [ch for b in qc["duplicate_blocks"] for ch in b["channels"]]
        unknown = sorted(set(used) - known)
        if unknown:
            raise CrpError(f"{path}: channel(s) not in the mapping: {unknown}")
    return qc


def load_corrections(
    path: Path = DEFAULT_CORRECTIONS, mapping: Mapping | None = None
) -> list[dict[str, Any]]:
    """Human-confirmed corrections. An entry without non-empty evidence is refused."""
    entries = _read_yaml(path).get("corrections") or []
    if not isinstance(entries, list):
        raise CrpError(f"{path}: `corrections` must be a list")
    out: list[dict[str, Any]] = []
    for i, entry in enumerate(entries, start=1):
        where = f"{path}: correction #{i}"
        if not isinstance(entry, dict):
            raise CrpError(f"{where}: must be a mapping")
        unknown = sorted(set(entry) - {"station", "date", "channel", "action", "value", "evidence"})
        if unknown:
            raise CrpError(f"{where}: unknown field(s) {unknown}")
        evidence = entry.get("evidence")
        if not isinstance(evidence, str) or not evidence.strip():
            raise CrpError(f"{where}: refused, `evidence` is missing or empty")
        for key in ("station", "date", "channel", "action"):
            if entry.get(key) in (None, ""):
                raise CrpError(f"{where}: `{key}` is missing")
        action = entry["action"]
        if action not in CORRECTION_ACTIONS:
            raise CrpError(f"{where}: action must be one of {CORRECTION_ACTIONS}, got {action!r}")
        try:
            date = dt.date.fromisoformat(str(entry["date"]))
        except ValueError as exc:
            raise CrpError(f"{where}: date must be YYYY-MM-DD") from exc
        value = entry.get("value")
        if action == "replace":
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise CrpError(f"{where}: action replace needs a numeric `value`")
        elif value is not None:
            raise CrpError(f"{where}: `value` is only allowed with action replace")
        if mapping is not None and entry["channel"] not in mapping.channels:
            raise CrpError(f"{where}: channel {entry['channel']!r} is not in the mapping")
        out.append(
            {
                "station": str(entry["station"]),
                "date": date,
                "channel": str(entry["channel"]),
                "action": action,
                "value": None if value is None else float(value),
                "evidence": evidence.strip(),
            }
        )
    return out


# --------------------------------------------------------------------------------------
# Cells, dates, headers
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Cell:
    """One source cell. kind: missing | numeric | left | right | text.

    ``number`` is the number as written in the file (for censored cells, the limit).
    """

    raw: str
    kind: str
    number: float | None = None

    @property
    def is_value(self) -> bool:
        return self.kind in ("numeric", "left", "right")


def parse_cell(raw: str, missing_markers: frozenset[str] = frozenset({"", "\\"})) -> Cell:
    text = raw.strip()
    if text in missing_markers:
        return Cell(raw, "missing")
    if RE_NUMBER.match(text):
        return Cell(raw, "numeric", float(text))
    m = RE_CENSORED.match(text)
    if m:
        return Cell(raw, "left" if m.group(1) == "<" else "right", float(m.group(2)))
    return Cell(raw, "text")


def parse_date(text: str, pivot: int = 70) -> dt.date | None:
    """m/d/yyyy, mm/dd/yy or mm/dd/yyyy. Anything else (e.g. a 3-digit year) is None."""
    m = RE_DATE.match(text.strip())
    if not m:
        return None
    month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if len(m.group(3)) == 2:
        year += 1900 if year >= pivot else 2000
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def normalize_header(text: str) -> str:
    """Lowercase, drop units in parentheses, collapse punctuation and spaces."""
    text = re.sub(r"\([^)]*\)", " ", text).lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


# --------------------------------------------------------------------------------------
# Reading files
# --------------------------------------------------------------------------------------


@dataclass
class RawRow:
    line: int  # file line (header = 1)
    cells: list[str]
    text: str  # original row text


def _cell_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dt.datetime | dt.date):
        return f"{value.month}/{value.day}/{value.year}"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _rows_to_raw(rows: list[list[Any]]) -> list[RawRow]:
    out = []
    for i, row in enumerate(rows, start=1):
        cells = [_cell_to_text(v) for v in row]
        buf = io.StringIO()
        csv.writer(buf, lineterminator="").writerow(cells)
        out.append(RawRow(i, cells, buf.getvalue()))
    return out


def read_table(path: Path) -> list[RawRow]:
    """Rows of one export as text cells, with file line numbers."""
    suffix = path.suffix.lower()
    if suffix == ".csv":
        text = path.read_bytes().decode("utf-8-sig")
        lines = text.splitlines()
        reader = csv.reader(io.StringIO(text, newline=""))
        out, start = [], 1
        for cells in reader:
            end = reader.line_num
            out.append(RawRow(start, cells, "\n".join(lines[start - 1 : end])))
            start = end + 1
        return out
    if suffix == ".xlsx":
        try:
            import openpyxl
        except ImportError as exc:
            raise CrpError(f"{path.name}: reading .xlsx needs openpyxl (make setup-ml)") from exc
        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
        if len(book.worksheets) != 1:
            raise CrpError(f"{path.name}: expected one sheet, found {len(book.worksheets)}")
        return _rows_to_raw([list(r) for r in book.worksheets[0].iter_rows(values_only=True)])
    if suffix == ".xls":
        try:
            import xlrd
        except ImportError as exc:
            raise CrpError(f"{path.name}: reading .xls needs xlrd (not installed)") from exc
        book = xlrd.open_workbook(str(path))
        if book.nsheets != 1:
            raise CrpError(f"{path.name}: expected one sheet, found {book.nsheets}")
        sheet = book.sheet_by_index(0)
        rows = []
        for r in range(sheet.nrows):
            row = []
            for c in range(sheet.ncols):
                cell = sheet.cell(r, c)
                if cell.ctype == xlrd.XL_CELL_DATE:
                    row.append(xlrd.xldate_as_datetime(cell.value, book.datemode))
                else:
                    row.append(cell.value if cell.value != "" else None)
            rows.append(row)
        return _rows_to_raw(rows)
    raise CrpError(f"{path.name}: unsupported file type")


def list_raw_files(raw_dir: Path, mapping: Mapping) -> list[Path]:
    """Files in the raw folder (hidden dot-files ignored). Stops on a bad name or empty folder."""
    if not raw_dir.is_dir():
        raise CrpError(f"{raw_dir} does not exist")
    files = sorted(p for p in raw_dir.iterdir() if p.is_file() and not p.name.startswith("."))
    if not files:
        raise CrpError(f"{raw_dir} is empty")
    bad = [p.name for p in files if not mapping.file_pattern.match(p.name)]
    if bad:
        raise CrpError(f"file name(s) not matching {mapping.file_pattern.pattern}: {bad}")
    stems = Counter(p.stem for p in files)
    dupes = sorted(s for s, n in stems.items() if n > 1)
    if dupes:
        raise CrpError(f"more than one file for station(s) {dupes}")
    return files


@dataclass
class DataRow:
    station: str
    line: int
    text: str
    date_raw: str
    date: dt.date | None
    cells: dict[str, Cell]  # channel -> cell, in column order


@dataclass
class ParsedFile:
    station: str
    path: Path
    sha256: str
    size: int
    n_lines: int
    headers: list[tuple[str, Column | None]]  # raw header text, mapped column (None = date)
    empty_header_columns: list[int]  # 1-based positions of empty header cells
    rows: list[DataRow]
    footers: list[RawRow]
    n_blank: int


def parse_file(path: Path, mapping: Mapping) -> ParsedFile:
    station = path.stem
    blob = path.read_bytes()
    table = read_table(path)
    if not table:
        raise CrpError(f"{path.name}: file has no rows")
    header = table[0].cells
    by_header = mapping.by_header
    if normalize_header(header[0]) != mapping.date_header:
        raise CrpError(f"{path.name}: first column is {header[0]!r}, expected the date column")
    headers: list[tuple[str, Column | None]] = [(header[0], None)]
    col_index: dict[int, Column] = {}
    empty_cols: list[int] = []
    for i, cell in enumerate(header[1:], start=1):
        if not cell.strip():
            empty_cols.append(i + 1)
            continue
        col = by_header.get(normalize_header(cell))
        if col is None:
            raise CrpError(
                f"{path.name}: column {i + 1} {cell!r} (normalized "
                f"{normalize_header(cell)!r}) is not in the mapping"
            )
        if col in col_index.values():
            raise CrpError(f"{path.name}: column {cell!r} appears twice")
        col_index[i] = col
        headers.append((cell, col))
    missing = [c.header for c in mapping.columns if c not in col_index.values()]
    if missing:
        raise CrpError(f"{path.name}: mapped column(s) not found in the file: {missing}")

    footer_labels = set(mapping.summary_rows) | set(mapping.disclaimer_rows)
    rows: list[DataRow] = []
    footers: list[RawRow] = []
    n_blank = 0
    for raw in table[1:]:
        for i, cell in enumerate(raw.cells):
            if cell.strip() and i not in col_index and i != 0:
                raise CrpError(
                    f"{path.name} line {raw.line}: cell {cell!r} in column {i + 1}, "
                    "which has no header"
                )
        if not any(c.strip() for c in raw.cells):
            n_blank += 1
            continue
        first = raw.cells[0].strip()
        if normalize_header(first) in footer_labels:
            footers.append(raw)
            continue
        cells = {
            col.channel: parse_cell(
                raw.cells[i] if i < len(raw.cells) else "", mapping.missing_markers
            )
            for i, col in sorted(col_index.items())
        }
        date = parse_date(first, mapping.two_digit_year_pivot)
        rows.append(DataRow(station, raw.line, raw.text, first, date, cells))
    return ParsedFile(
        station=station,
        path=path,
        sha256=hashlib.sha256(blob).hexdigest(),
        size=len(blob),
        n_lines=len(blob.decode("utf-8-sig").splitlines()) if path.suffix == ".csv" else len(table),
        headers=headers,
        empty_header_columns=empty_cols,
        rows=rows,
        footers=footers,
        n_blank=n_blank,
    )


# --------------------------------------------------------------------------------------
# Pipeline: quarantine, merge, screening flags
# --------------------------------------------------------------------------------------


@dataclass
class Quarantine:
    station: str
    line: int
    text: str
    reason: str
    column: str = ""  # set when a single value (not the whole row) is quarantined
    value_raw: str = ""
    n_cells: int = 0  # numeric or censored cells held by this entry


@dataclass
class Note:
    station: str
    line: int
    column: str
    text: str


@dataclass
class Value:
    station: str
    date: dt.date
    channel: str
    cell: Cell
    line: int
    reasons: list[str] = field(default_factory=list)
    human: str = ""  # "replace" or "confirm_ok" when a human-confirmed correction applies
    override: float | None = None  # replacement number (unit of the file) for "replace"

    @property
    def quality(self) -> str:
        return "suspect" if self.reasons and not self.human else "good"

    @property
    def flag_reason(self) -> str:
        if self.human:
            return {"replace": "corrected", "confirm_ok": "confirmed_ok"}[self.human]
        return ";".join(self.reasons)

    def add_reason(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)


@dataclass
class Sample:
    station: str
    date: dt.date
    lines: list[int]
    values: dict[str, Value]
    in_clean: bool = True  # False for a quarantined row that is only screened for the report


@dataclass
class Flag:
    station: str
    line: int
    date: dt.date | None
    column: str
    reason: str
    value_raw: str
    detail: str = ""
    in_clean: bool = True


@dataclass
class Result:
    files: list[ParsedFile]
    samples: list[Sample]
    quarantine: list[Quarantine]
    notes: list[Note]
    flags: list[Flag]
    merges: list[str]
    unresolved_order_breaks: list[str]
    cells_parsed: int  # numeric + censored + text cells on data rows
    corrections_applied: list[str] = field(default_factory=list)


def out_of_sequence(dates: list[dt.date]) -> set[int]:
    """Indices whose date lies outside [previous, next] while previous <= next."""
    return {
        i
        for i in range(1, len(dates) - 1)
        if dates[i - 1] <= dates[i + 1] and not dates[i - 1] <= dates[i] <= dates[i + 1]
    }


def _signature(cell: Cell | None) -> str:
    if cell is None or not cell.is_value:
        return ""
    return {"numeric": "", "left": "<", "right": ">"}[cell.kind] + repr(cell.number)


def _process_file(pf: ParsedFile, qc: dict[str, Any], res: Result) -> None:
    rules = qc["row_quarantine"]
    conflict_reason = qc["same_date_merge"]["conflict_reason"]

    for row in pf.rows:
        for channel, cell in row.cells.items():
            if cell.kind == "text":
                res.notes.append(Note(pf.station, row.line, channel, cell.raw.strip()))
            if cell.kind != "missing":
                res.cells_parsed += 1

    def whole_row(row: DataRow, reason: str) -> None:
        n = sum(c.is_value for c in row.cells.values())
        res.quarantine.append(Quarantine(pf.station, row.line, row.text, reason, n_cells=n))

    dated = []
    for row in pf.rows:
        if row.date is None:
            if not rules.get("date_unparseable", True):
                raise CrpError(
                    f"{pf.path.name} line {row.line}: date {row.date_raw!r} cannot be parsed "
                    "and row_quarantine.date_unparseable is off"
                )
            whole_row(row, "date_unparseable")
        else:
            dated.append(row)

    bad = (
        out_of_sequence([r.date for r in dated])
        if rules.get("date_out_of_sequence", True)
        else set()
    )
    kept = [r for i, r in enumerate(dated) if i not in bad]
    screened_only = [r for i, r in enumerate(dated) if i in bad]
    for row in screened_only:
        whole_row(row, "date_out_of_sequence")
    for a, b in zip(kept, kept[1:], strict=False):
        if a.date > b.date:
            res.unresolved_order_breaks.append(
                f"{pf.station}: line {a.line} ({a.date}) is followed by line {b.line} ({b.date})"
            )

    # Merge rows sharing (station, date): a value fills a blank; a different value is a conflict.
    by_date: dict[dt.date, Sample] = {}
    for row in kept:
        sample = by_date.get(row.date)
        if sample is None:
            sample = by_date[row.date] = Sample(pf.station, row.date, [], {})
        sample.lines.append(row.line)
        filled, same, conflicts = [], [], []
        for channel, cell in row.cells.items():
            if not cell.is_value:
                continue
            first = sample.values.get(channel)
            if first is None:
                sample.values[channel] = Value(pf.station, row.date, channel, cell, row.line)
                filled.append(channel)
                continue
            raw = cell.raw.strip()
            if _signature(first.cell) == _signature(cell):
                same.append(channel)
                reason = "duplicate_same_value"
            else:
                conflicts.append(
                    f"{channel}: kept {first.cell.raw.strip()!r} (line {first.line}), "
                    f"quarantined {raw!r} (line {row.line})"
                )
                reason = conflict_reason
                first.add_reason(conflict_reason)
                res.flags.append(
                    Flag(
                        pf.station,
                        first.line,
                        row.date,
                        channel,
                        conflict_reason,
                        first.cell.raw.strip(),
                        f"line {row.line} says {raw!r}",
                    )
                )
            res.quarantine.append(
                Quarantine(pf.station, row.line, row.text, reason, channel, raw, n_cells=1)
            )
        if len(sample.lines) > 1:
            res.merges.append(
                f"{pf.station} {row.date}: line {row.line} merged into line {sample.lines[0]}; "
                f"filled {filled or 'nothing'}; identical {same or 'nothing'}; "
                f"conflicts {conflicts or 'none'}"
            )
    samples = sorted(by_date.values(), key=lambda s: s.date)

    # Value-level screening (values are kept, only marked suspect).
    for sample in samples:
        for rule in qc["range_rules"]:
            v = sample.values.get(rule["channel"])
            if v is None or v.cell.kind != "numeric":
                continue
            lo, hi = rule.get("min"), rule.get("max")
            if (lo is not None and v.cell.number < lo) or (hi is not None and v.cell.number > hi):
                v.add_reason(rule["reason"])
                bounds = f"{'' if lo is None else lo} to {'' if hi is None else hi}".strip()
                res.flags.append(
                    Flag(
                        pf.station,
                        v.line,
                        sample.date,
                        v.channel,
                        rule["reason"],
                        v.cell.raw.strip(),
                        f"screen {bounds}",
                    )
                )
        for rule in qc["equals_before_rules"]:
            v = sample.values.get(rule["channel"])
            if (
                v is not None
                and v.cell.kind == "numeric"
                and v.cell.number == rule["equals"]
                and sample.date < rule["before"]
            ):
                v.add_reason(rule["reason"])
                res.flags.append(
                    Flag(
                        pf.station,
                        v.line,
                        sample.date,
                        v.channel,
                        rule["reason"],
                        v.cell.raw.strip(),
                        f"equals {rule['equals']} before {rule['before']}",
                    )
                )

    # Duplicate blocks. Quarantined out-of-sequence rows are screened too, for the report only.
    extra = [
        Sample(
            pf.station,
            r.date,
            [r.line],
            {
                ch: Value(pf.station, r.date, ch, c, r.line)
                for ch, c in r.cells.items()
                if c.is_value
            },
            in_clean=False,
        )
        for r in screened_only
    ]
    ordered = sorted(samples + extra, key=lambda s: (s.date, s.lines[0]))
    for block in qc["duplicate_blocks"]:
        seen: dict[tuple[str, ...], Sample] = {}
        for sample in ordered:
            vals = [sample.values.get(ch) for ch in block["channels"]]
            n_filled = sum(v is not None for v in vals)
            if n_filled < block["min_filled"]:
                continue
            sig = tuple(_signature(v.cell if v else None) for v in vals)
            first = seen.setdefault(sig, sample)
            if first is sample or first.date == sample.date:
                continue
            for v in vals:
                if v is not None:
                    v.add_reason(block["reason"])
            res.flags.append(
                Flag(
                    pf.station,
                    sample.lines[0],
                    sample.date,
                    f"[{block['name']} block]",
                    block["reason"],
                    f"{n_filled} cells",
                    f"identical to line {first.lines[0]} ({first.date})"
                    + ("" if sample.in_clean else "; this row is quarantined"),
                    in_clean=sample.in_clean,
                )
            )
    res.samples.extend(samples)


def _apply_corrections(res: Result, corrections: list[dict[str, Any]]) -> None:
    """Apply human-confirmed corrections. One that matches no clean value stops the run."""
    samples = {(x.station, x.date): x for x in res.samples}
    texts = {(pf.station, r.line): r.text for pf in res.files for r in pf.rows}
    for c in corrections:
        where = f"correction {c['station']} {c['date']} {c['channel']}"
        sample = samples.get((c["station"], c["date"]))
        v = sample.values.get(c["channel"]) if sample else None
        if v is None:
            raise CrpError(f"{where}: no such value in the clean table")
        raw = v.cell.raw.strip()
        if c["action"] == "drop":
            del sample.values[c["channel"]]
            res.quarantine.append(
                Quarantine(
                    v.station,
                    v.line,
                    texts[(v.station, v.line)],
                    "dropped_by_correction",
                    v.channel,
                    raw,
                    n_cells=1,
                )
            )
        else:
            v.human = c["action"]
            v.override = c["value"]
        res.corrections_applied.append(
            f"{where} (line {v.line}, file value {raw!r}): {c['action']}"
            + (f" -> {c['value']}" if c["action"] == "replace" else "")
            + f"; evidence: {c['evidence']}"
        )


def run_pipeline(
    raw_dir: Path,
    mapping: Mapping,
    qc: dict[str, Any],
    corrections: list[dict[str, Any]] | None = None,
) -> Result:
    res = Result([], [], [], [], [], [], [], 0)
    for path in list_raw_files(raw_dir, mapping):
        pf = parse_file(path, mapping)
        res.files.append(pf)
        _process_file(pf, qc, res)
    _apply_corrections(res, corrections or [])
    return res


# --------------------------------------------------------------------------------------
# inspect: docs/crp_schema.md and the screening report
# --------------------------------------------------------------------------------------


def percentile(values: list[float], q: float) -> float:
    """Linear-interpolation percentile (q in 0..100) of a non-empty list."""
    xs = sorted(values)
    pos = (len(xs) - 1) * q / 100
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    def esc(v: Any) -> str:
        return str(v).replace("|", "\\|")

    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(esc(v) for v in row) + " |" for row in rows]
    return "\n".join(out)


def _lines(nums: list[int]) -> str:
    return ", ".join(str(n) for n in nums)


def _date_format(text: str) -> str:
    m = RE_DATE.match(text.strip())
    if not m:
        return "other"
    if len(m.group(3)) == 2:
        return "mm/dd/yy"
    padded = len(m.group(1)) == 2 and len(m.group(2)) == 2
    return "mm/dd/yyyy" if padded else "m/d/yyyy"


def render_schema(res: Result, mapping: Mapping, today: dt.date) -> str:
    files = res.files
    meta = ", ".join(f"{k}={str(v).lower()}" for k, v in mapping.date_meta.items())
    stations = [pf.station for pf in files]
    out: list[str] = []
    add = out.append
    add("# CRP exports: schema, units and cell encodings")
    add("")
    add(
        f"Generated by `python -m ml.data.crp inspect` on {today.isoformat()}. Every number below "
        "is computed from the files; do not edit this file by hand. Counts are taken before any "
        "quarantine, merge or screening rule."
    )
    add("")
    add(
        md_table(
            ["file", "SHA-256", "bytes", "lines"],
            [[pf.path.name, f"`{pf.sha256}`", pf.size, pf.n_lines] for pf in files],
        )
    )
    add("")
    add("## Status of the data")
    add("")
    add("The files carry these footer lines, copied verbatim (station, file line):")
    add("")
    for pf in files:
        for raw in pf.footers:
            if normalize_header(raw.cells[0]) in mapping.disclaimer_rows:
                add(f"- {pf.station} line {raw.line}: `{raw.cells[0].strip()}`")
    add("")
    add("These exports are working copies, not QA-approved data.")
    add("")
    add("## Stations")
    add("")
    add(
        md_table(
            ["station", "node_id", "description", "source of description"],
            [
                [
                    s,
                    mapping.node_id_template.format(station=s),
                    mapping.stations.get(s, {}).get("description", "PLACEHOLDER (not in mapping)"),
                    mapping.stations.get(s, {}).get("description_source", "PLACEHOLDER"),
                ]
                for s in stations
            ],
        )
    )
    add("")
    add("The station id comes only from the file name.")
    add("")
    add("## Rows")
    add("")
    rows = []
    for pf in files:
        dates = [r.date for r in pf.rows if r.date]
        fmts = Counter(_date_format(r.date_raw) for r in pf.rows)
        rows.append(
            [
                pf.station,
                pf.n_lines,
                len(pf.headers),
                _lines(pf.empty_header_columns) or "none",
                len(pf.rows),
                len(dates),
                len(pf.rows) - len(dates),
                pf.n_blank,
                len(pf.footers),
                min(dates).isoformat(),
                max(dates).isoformat(),
                fmts["m/d/yyyy"],
                fmts["mm/dd/yyyy"],
                fmts["mm/dd/yy"],
                fmts["other"],
            ]
        )
    add(
        md_table(
            [
                "station",
                "file lines",
                "named columns",
                "empty header cells (position)",
                "data rows",
                "parseable date",
                "unparseable date",
                "blank lines",
                "footer lines",
                "first date",
                "last date",
                "m/d/yyyy",
                "mm/dd/yyyy",
                "mm/dd/yy",
                "other date text",
            ],
            rows,
        )
    )
    add("")
    add(
        "A data row is any line after the header that is not blank and whose first cell is not a "
        "footer label. A column whose header cell is empty holds no data on any line (checked on "
        "every line; the run stops otherwise). First and last date include rows that the cleaning "
        "step later quarantines."
    )
    add("")
    for pf in files:
        for r in pf.rows:
            if r.date is None:
                add(f"- Unparseable date: {pf.station} line {r.line}: `{r.date_raw}`")
    add("")
    add(
        f"Dates have no time of day. Each is stored as that date at {mapping.stored_time_utc} UTC "
        f"with meta `{meta}`. Two-digit years: yy >= {mapping.two_digit_year_pivot} is "
        "19yy, otherwise 20yy."
    )
    add("")
    add("## Columns and unit assumptions")
    add("")
    rows = []
    for i, col in enumerate(mapping.columns, start=2):
        texts = []
        for pf in files:
            texts += [h for h, c in pf.headers if c is col]
        stored = col.unit + (
            f" (file: {col.source_unit} x {col.factor})" if col.source_unit else ""
        )
        rows.append(
            [
                i,
                " / ".join(f"`{t}`" for t in dict.fromkeys(texts)),
                f"`{col.channel}`",
                stored,
                col.kind,
                col.note,
            ]
        )
    add(md_table(["#", "header text in the files", "channel", "stored unit", "kind", "note"], rows))
    add("")
    add(
        "Column 1 is the date. The column order is the same in every file: "
        + (
            "yes"
            if len({tuple(c.channel for _, c in pf.headers if c) for pf in files}) == 1
            else "NO"
        )
        + "."
    )
    add("")
    cond = next((c for c in mapping.columns if c.channel == "crp_specific_conductance"), None)
    if cond is not None:
        rows = []
        for pf in files:
            xs = [
                r.cells[cond.channel].number
                for r in pf.rows
                if r.cells[cond.channel].kind == "numeric"
            ]
            if xs:
                rows.append(
                    [
                        pf.station,
                        len(xs),
                        min(xs),
                        round(percentile(xs, 5), 1),
                        round(percentile(xs, 50), 1),
                        round(percentile(xs, 95), 1),
                        max(xs),
                    ]
                )
        add(
            "Specific conductance: the header says S/cm, but the numbers have the size of "
            "microsiemens per cm, so the stored unit is uS/cm (assumption):"
        )
        add("")
        add(md_table(["station", "n numeric", "min", "p5", "p50", "p95", "max"], rows))
        add("")
    add("## Cell encodings")
    add("")
    add("- blank or `\\`: missing.")
    add("- `<x`, `< x`, `<.05`: below detection, left-censored at x. `>x`, `> x`: right-censored.")
    add(
        "- Any other non-numeric text (including a number with a leading `*`) is never turned "
        "into a number; it is recorded in `crp_notes.csv`."
    )
    add("")
    rows = []
    for pf in files:
        cells = [c for r in pf.rows for c in r.cells.values()]
        raw = [c.raw.strip() for c in cells]
        rows.append(
            [
                pf.station,
                sum(t == "" for t in raw),
                sum(t == "\\" for t in raw),
                sum(c.kind == "numeric" for c in cells),
                sum(bool(re.match(r"^<\S", t)) for t in raw),
                sum(t.startswith("< ") for t in raw),
                sum(bool(re.match(r"^>\S", t)) for t in raw),
                sum(t.startswith("> ") for t in raw),
                sum(bool(re.match(r"^[<>]\s*\.", t)) for t in raw),
                sum(c.kind == "text" for c in cells),
            ]
        )
    add(
        md_table(
            [
                "station",
                "blank",
                "`\\`",
                "numeric",
                "`<x`",
                "`< x`",
                "`>x`",
                "`> x`",
                "censored with no leading zero",
                "text",
            ],
            rows,
        )
    )
    add("")
    add("Text cells (every distinct text, with file lines):")
    add("")
    rows = []
    for pf in files:
        seen: dict[tuple[str, str], list[int]] = {}
        for r in pf.rows:
            for ch, c in r.cells.items():
                if c.kind == "text":
                    seen.setdefault((ch, c.raw.strip()), []).append(r.line)
        rows += [
            [pf.station, f"`{ch}`", f"`{t}`", len(ls), _lines(ls)] for (ch, t), ls in seen.items()
        ]
    add(md_table(["station", "column", "text", "cells", "file lines"], rows))
    add("")
    add("## Footer rows (not data)")
    add("")
    rows = []
    for pf in files:
        for raw in pf.footers:
            others = [
                f"{pf.headers[i][0]}={c.strip()}"
                for i, c in enumerate(raw.cells)
                if i and c.strip() and i < len(pf.headers)
            ]
            rows.append(
                [
                    pf.station,
                    raw.line,
                    f"`{raw.cells[0].strip()}`",
                    "; ".join(others) or "(no other cells)",
                ]
            )
    add(md_table(["station", "file line", "first cell", "other cells (ignored)"], rows))
    add("")
    add("## Cell counts per column per station")
    add("")
    add("numeric / left-censored / right-censored / missing / text, over all data rows.")
    add("")
    rows = []
    for col in mapping.columns:
        row: list[Any] = [f"`{col.channel}`"]
        for pf in files:
            k = Counter(r.cells[col.channel].kind for r in pf.rows)
            row.append(
                f"{k['numeric']} / {k['left']} / {k['right']} / {k['missing']} / {k['text']}"
            )
        rows.append(row)
    add(md_table(["channel", *stations], rows))
    add("")
    add("## Labels per year")
    add("")
    add(
        "numeric + censored cells per calendar year, rows with a parseable date only, before "
        "quarantine. Gaps between samples are real and are never filled."
    )
    for col in (c for c in mapping.columns if c.kind == "label"):
        counts: dict[str, Counter[int]] = {}
        undated: dict[str, int] = {}
        for pf in files:
            counts[pf.station] = Counter(
                r.date.year for r in pf.rows if r.date and r.cells[col.channel].is_value
            )
            undated[pf.station] = sum(
                r.date is None and r.cells[col.channel].is_value for r in pf.rows
            )
        years = sorted({y for c in counts.values() for y in c})
        add("")
        add(f"`{col.channel}` ({col.unit}):")
        add("")
        rows = [[y, *[counts[s][y] for s in stations]] for y in years]
        rows.append(["total", *[sum(counts[s].values()) for s in stations]])
        rows.append(["on unparseable-date rows", *[undated[s] for s in stations]])
        add(md_table(["year", *stations], rows))
    add("")
    add("## Screening summary")
    add("")
    add(
        "Counts from the detectors in `ml/data/crp_qc.yaml` (all thresholds are PLACEHOLDERs). "
        "The line-by-line list is printed by `python -m ml.data.crp inspect`."
    )
    add("")
    q = Counter((x.station, x.reason) for x in res.quarantine)
    f = Counter((x.station, x.reason) for x in res.flags)
    rows = [
        [f"{label}: {r}", *[counts[(s, r)] for s in stations]]
        for label, counts in (("quarantine", q), ("suspect", f))
        for r in sorted({reason for _, reason in counts})
    ]
    rows.append(["text cells (notes)", *[sum(n.station == s for n in res.notes) for s in stations]])
    add(md_table(["reason", *stations], rows))
    add("")
    return "\n".join(out)


def render_flags(res: Result) -> str:
    """Everything the detectors found, grouped by reason, with file line numbers."""
    out: list[str] = []
    groups: dict[str, list[list[Any]]] = {}
    for x in res.quarantine:
        what = "whole row" if not x.column else f"{x.column}={x.value_raw}"
        date = x.text.split(",", 1)[0]
        groups.setdefault(f"QUARANTINE {x.reason}", []).append(
            [x.station, x.line, date, what, f"{x.n_cells} value cell(s)"]
        )
    for x in res.flags:
        groups.setdefault(f"SUSPECT {x.reason}", []).append(
            [x.station, x.line, x.date, x.column, x.value_raw, x.detail]
        )
    for reason in sorted(groups):
        rows = sorted(groups[reason], key=lambda r: (r[0], r[1]))
        out.append(f"\n== {reason} ({len(rows)})")
        widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
        out += [
            "  " + "  ".join(str(v).ljust(w) for v, w in zip(r, widths, strict=True)).rstrip()
            for r in rows
        ]
    out.append(f"\n== SAME-DATE MERGES ({len(res.merges)})")
    out += [f"  {m}" for m in res.merges]
    out.append(f"\n== ORDER BREAKS NOT RESOLVED BY QUARANTINE ({len(res.unresolved_order_breaks)})")
    out += [f"  {m}" for m in res.unresolved_order_breaks]
    notes: dict[tuple[str, str, str], list[int]] = {}
    for n in res.notes:
        notes.setdefault((n.station, n.column, n.text), []).append(n.line)
    out.append(f"\n== TEXT CELLS -> NOTES ({len(res.notes)})")
    out += [
        f"  {s}  {col}  {text!r}  x{len(ls)}  lines {_lines(ls)}"
        for (s, col, text), ls in notes.items()
    ]
    return "\n".join(out)


# --------------------------------------------------------------------------------------
# clean: crp_long.csv, quarantine, notes, profile, report (identical on every run)
# --------------------------------------------------------------------------------------

LONG_COLUMNS = [
    "ts_utc",
    "station",
    "node_id",
    "channel",
    "value",
    "unit",
    "kind",
    "source",
    "quality",
    "flag_reason",
    "censored",
    "detection_limit",
    "value_raw",
    "sample_id",
    "src_file",
    "src_line",
    "meta",
]
QUARANTINE_COLUMNS = [
    "station",
    "src_line",
    "original_row_text",
    "reason",
    "column",
    "value_raw",
    "n_cells",
]
NOTES_COLUMNS = ["station", "src_line", "column", "text"]
PROFILE_NOTE = (
    "Statistical summary of real IBWC CRP grab samples (source=CRP, quality=good rows only). "
    "It describes real samples and was not used to generate any data. Working copies, not "
    "QA-approved. Left-censored values enter at detection_limit / 2; nothing is interpolated."
)


def fmt_num(x: float) -> str:
    return f"{x:.10g}"


def stored_value(cell: Cell, factor: float = 1.0) -> tuple[float, float | None, str]:
    """(value, detection_limit, censored) as stored: left-censored is limit / 2."""
    n = cell.number * factor
    if cell.kind == "left":
        return n / 2, n, "left"
    if cell.kind == "right":
        return n, n, "right"
    return n, None, "none"


def long_records(res: Result, mapping: Mapping) -> list[dict[str, Any]]:
    """Rows of crp_long.csv (plus `date` and numeric `num` for the statistics)."""
    names = {pf.station: pf.path.name for pf in res.files}
    order = {c.channel: i for i, c in enumerate(mapping.columns)}
    out: list[dict[str, Any]] = []
    for sample in sorted(res.samples, key=lambda x: (x.station, x.date)):
        for v in sorted(sample.values.values(), key=lambda x: order[x.channel]):
            col = mapping.column(v.channel)
            if v.human == "replace":
                value, limit, censored = v.override * col.factor, None, "none"
            else:
                value, limit, censored = stored_value(v.cell, col.factor)
            meta = dict(mapping.date_meta)
            if col.source_unit:
                meta["original_unit"] = col.source_unit
            out.append(
                {
                    "ts_utc": f"{sample.date.isoformat()}T{mapping.stored_time_utc}Z",
                    "station": v.station,
                    "node_id": mapping.node_id_template.format(station=v.station),
                    "channel": v.channel,
                    "value": fmt_num(value),
                    "unit": col.unit,
                    "kind": col.kind,
                    "source": mapping.source,
                    "quality": v.quality,
                    "flag_reason": v.flag_reason,
                    "censored": censored,
                    "detection_limit": "" if limit is None else fmt_num(limit),
                    "value_raw": v.cell.raw.strip(),
                    "sample_id": f"{v.station}-{sample.date:%Y%m%d}",
                    "src_file": names[v.station],
                    "src_line": v.line,
                    "meta": json.dumps(meta, sort_keys=True),
                    "date": sample.date,
                    "num": value,
                }
            )
    return out


def _gaps(dates: list[dt.date]) -> list[int]:
    ds = sorted(set(dates))
    return [(b - a).days for a, b in zip(ds, ds[1:], strict=False)]


def _median(xs: list[float]) -> float | None:
    return percentile(xs, 50) if xs else None


def build_profile(records: list[dict[str, Any]], res: Result, mapping: Mapping) -> dict[str, Any]:
    cfg = mapping.profile
    min_n, channel, thr = cfg["min_n"], cfg["event"]["channel"], cfg["event"]["threshold"]
    good = [r for r in records if r["quality"] == "good"]

    def r6(x: float) -> float:
        return round(x, 6)

    def share(rs: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(rs)
        return {
            "n": n,
            "share_at_or_above": r6(sum(r["num"] >= thr for r in rs) / n) if n >= min_n else None,
        }

    def quantiles(xs: list[float]) -> dict[str, Any]:
        ok = len(xs) >= min_n
        return {
            "n": len(xs),
            **{f"p{q}": r6(percentile(xs, q)) if ok else None for q in (10, 50, 90)},
        }

    def block(rs: list[dict[str, Any]], gaps: list[int]) -> dict[str, Any]:
        labels = [r for r in rs if r["channel"] == channel]
        years = Counter(r["date"].year for r in labels)
        ok = len(gaps) >= min_n
        return {
            "n_labels": len(labels),
            "n_labels_left_censored": sum(r["censored"] == "left" for r in labels),
            "labels_per_year": {str(y): years[y] for y in sorted(years)},
            "event_share_by_month": {
                str(m): share([r for r in labels if r["date"].month == m]) for m in range(1, 13)
            },
            "event_share_by_season": {
                name: share([r for r in labels if r["date"].month in months])
                for name, months in cfg["seasons"].items()
            },
            "days_between_labels": {
                "n_gaps": len(gaps),
                "median": r6(percentile(gaps, 50)) if ok else None,
                "q1": r6(percentile(gaps, 25)) if ok else None,
                "q3": r6(percentile(gaps, 75)) if ok else None,
            },
            "monthly": {
                ch: {
                    "unit": mapping.column(ch).unit,
                    **{
                        str(m): quantiles(
                            [r["num"] for r in rs if r["channel"] == ch and r["date"].month == m]
                        )
                        for m in range(1, 13)
                    },
                }
                for ch in cfg["monthly_channels"]
            },
        }

    stations: dict[str, Any] = {}
    all_gaps: list[int] = []
    for pf in res.files:
        rs = [r for r in good if r["station"] == pf.station]
        gaps = _gaps([r["date"] for r in rs if r["channel"] == channel])
        all_gaps += gaps
        stations[pf.station] = block(rs, gaps)
    stations["pooled"] = block(good, all_gaps)
    return {
        "note": PROFILE_NOTE,
        "source": mapping.source,
        "quality_filter": "good",
        "inputs": {pf.path.name: pf.sha256 for pf in res.files},
        "event": {"channel": channel, "threshold": thr, "unit": mapping.column(channel).unit},
        "min_n": min_n,
        "pooled_note": "pooled = all stations together; pooled gaps are the per-station gaps.",
        "stations": stations,
    }


def _quarantined_cells(res: Result, station: str, channel: str) -> list[Cell]:
    """Value cells of one channel that went to the quarantine file."""
    whole = {q.line for q in res.quarantine if q.station == station and not q.column}
    pf = next(f for f in res.files if f.station == station)
    cells = [r.cells[channel] for r in pf.rows if r.line in whole and r.cells[channel].is_value]
    cells += [
        parse_cell(q.value_raw)
        for q in res.quarantine
        if q.station == station and q.column == channel
    ]
    return cells


def label_table(res: Result, records: list[dict[str, Any]], mapping: Mapping) -> str:
    """Event-channel labels per station before and after quarantine and flags."""
    channel, thr = mapping.profile["event"]["channel"], mapping.profile["event"]["threshold"]
    stations = [pf.station for pf in res.files]
    rows: dict[str, list[Any]] = {}

    def put(name: str, value: Any) -> None:
        rows.setdefault(name, []).append(value)

    for pf in res.files:
        cells = [r.cells[channel] for r in pf.rows if r.cells[channel].is_value]
        quar = _quarantined_cells(res, pf.station, channel)
        clean = [r for r in records if r["station"] == pf.station and r["channel"] == channel]
        good = [r for r in clean if r["quality"] == "good"]
        put("in the file: numeric", sum(c.kind == "numeric" for c in cells))
        put("in the file: censored", sum(c.kind != "numeric" for c in cells))
        put(f"in the file: at or above {thr}", sum(stored_value(c)[0] >= thr for c in cells))
        put("quarantined", len(quar))
        put(f"quarantined: at or above {thr}", sum(stored_value(c)[0] >= thr for c in quar))
        put("clean table: numeric", sum(r["censored"] == "none" for r in clean))
        put("clean table: censored", sum(r["censored"] != "none" for r in clean))
        put(f"clean table: at or above {thr}", sum(r["num"] >= thr for r in clean))
        put("clean table: flagged suspect", len(clean) - len(good))
        put("good only: numeric", sum(r["censored"] == "none" for r in good))
        put("good only: censored", sum(r["censored"] != "none" for r in good))
        put(f"good only: at or above {thr}", sum(r["num"] >= thr for r in good))
        put(
            "distinct sample dates (clean table)", sum(x.station == pf.station for x in res.samples)
        )
        put("median days between labels (clean table)", _median(_gaps([r["date"] for r in clean])))
        put("median days between labels (good only)", _median(_gaps([r["date"] for r in good])))
    return md_table([f"{channel} labels", *stations], [[k, *v] for k, v in rows.items()])


def render_report(res: Result, records: list[dict[str, Any]], mapping: Mapping) -> str:
    cfg = mapping.profile
    channel = cfg["event"]["channel"]
    companions = cfg["companion_channels"]
    recent = dt.date.fromisoformat(str(cfg["recent_from"]))
    stations = [pf.station for pf in res.files]
    out: list[str] = []
    add = out.append
    add("# CRP cleaning report")
    add("")
    add(
        f"Real grab-sample data (source={mapping.source}). Working copies, not QA-approved. "
        "Nothing is interpolated, resampled or synthesized: gaps stay gaps. Screening flags mark "
        "a value suspect and keep it; all thresholds are PLACEHOLDERs. This report is generated by "
        "`python -m ml.data.crp clean` and is identical for identical inputs."
    )
    add("")
    add(
        md_table(
            ["file", "SHA-256", "bytes", "lines"],
            [[pf.path.name, f"`{pf.sha256}`", pf.size, pf.n_lines] for pf in res.files],
        )
    )
    add("")
    add("## Row and cell accounting")
    add("")
    rows: dict[str, list[Any]] = {}
    total_ok = True
    for pf in res.files:
        s = pf.station
        whole = [q for q in res.quarantine if q.station == s and not q.column]
        samples = [x for x in res.samples if x.station == s]
        parsed = sum(c.kind != "missing" for r in pf.rows for c in r.cells.values())
        n_long = sum(r["station"] == s for r in records)
        n_quar = sum(q.n_cells for q in res.quarantine if q.station == s)
        n_notes = sum(n.station == s for n in res.notes)
        ok = parsed == n_long + n_quar + n_notes
        total_ok = total_ok and ok
        for name, value in (
            ("data rows in the file", len(pf.rows)),
            ("rows with a parseable date", sum(r.date is not None for r in pf.rows)),
            (
                "rows quarantined: date_unparseable",
                sum(q.reason == "date_unparseable" for q in whole),
            ),
            (
                "rows quarantined: date_out_of_sequence",
                sum(q.reason == "date_out_of_sequence" for q in whole),
            ),
            (
                "rows merged into an earlier row of the same date",
                sum(len(x.lines) - 1 for x in samples),
            ),
            ("sample dates in the clean table", len(samples)),
            ("cells parsed (numeric + censored + text)", parsed),
            ("rows in crp_long.csv", n_long),
            ("value cells in crp_quarantine.csv", n_quar),
            ("rows in crp_notes.csv", n_notes),
            ("conservation (parsed = long + quarantine + notes)", "holds" if ok else "FAILS"),
        ):
            rows.setdefault(name, []).append(value)
    add(md_table(["", *stations], [[k, *v] for k, v in rows.items()]))
    add("")
    add("Data rows = sample dates + merged rows + quarantined rows, for every station.")
    add("")
    add(f"## {channel} labels before and after quarantine and flags")
    add("")
    add(label_table(res, records, mapping))
    add("")
    add("A left-censored label counts with value = detection_limit / 2.")
    add("")
    add(f"## Newest rows (dated {recent.isoformat()} or later) and same-day readings")
    add("")
    add(
        "Counted on the rows of the files, before quarantine. A reading counts when the cell on "
        "the same row is a plain number."
    )
    add("")
    rows = {}
    for pf in res.files:

        def has(r: DataRow, ch: str) -> bool:
            return r.cells[ch].kind == "numeric"

        new = [r for r in pf.rows if r.date and r.date >= recent]
        new_labels = [r for r in new if r.cells[channel].is_value]
        labels = [r for r in pf.rows if r.cells[channel].is_value]
        stats = [
            (f"rows dated {recent.year} or later", len(new)),
            (
                f"... with none of numeric {', '.join(companions)}",
                sum(not any(has(r, ch) for ch in companions) for r in new),
            ),
            (f"{channel} labels dated {recent.year} or later", len(new_labels)),
        ]
        stats += [
            (f"... without same-day {ch}", sum(not has(r, ch) for r in new_labels))
            for ch in companions
        ]
        stats += [(f"{channel} labels, all years", len(labels))]
        stats += [
            (f"... all years, without same-day {ch}", sum(not has(r, ch) for r in labels))
            for ch in companions
        ]
        stats += [
            (
                "... all years, without any of them",
                sum(not any(has(r, ch) for ch in companions) for r in labels),
            )
        ]
        for name, value in stats:
            rows.setdefault(name, []).append(value)
    add(md_table(["", *stations], [[k, *v] for k, v in rows.items()]))
    add("")
    add("## Corrections applied")
    add("")
    add(
        "\n".join(f"- {c}" for c in res.corrections_applied)
        or "None (crp_corrections.yaml is empty)."
    )
    add("")
    add("## Everything the detectors found")
    add("")
    add("```")
    add(render_flags(res).strip())
    add("```")
    add("")
    return "\n".join(out)


def _write_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, columns, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_clean(res: Result, mapping: Mapping, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    records = long_records(res, mapping)
    paths = [
        out_dir / name
        for name in (
            "crp_long.csv",
            "crp_quarantine.csv",
            "crp_notes.csv",
            "crp_profile.json",
            "crp_cleaning_report.md",
        )
    ]
    _write_csv(paths[0], LONG_COLUMNS, records)
    _write_csv(
        paths[1],
        QUARANTINE_COLUMNS,
        [
            {
                "station": q.station,
                "src_line": q.line,
                "original_row_text": q.text,
                "reason": q.reason,
                "column": q.column,
                "value_raw": q.value_raw,
                "n_cells": q.n_cells,
            }
            for q in sorted(res.quarantine, key=lambda q: (q.station, q.line, q.column))
        ],
    )
    _write_csv(
        paths[2],
        NOTES_COLUMNS,
        [
            {"station": n.station, "src_line": n.line, "column": n.column, "text": n.text}
            for n in res.notes
        ],
    )
    profile = build_profile(records, res, mapping)
    paths[3].write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    paths[4].write_text(render_report(res, records, mapping), encoding="utf-8")
    return paths


# --------------------------------------------------------------------------------------
# manifest and verify
# --------------------------------------------------------------------------------------

MANIFEST_HEADER = (
    "# Raw CRP files this repository was built against (the files themselves are gitignored).\n"
    "# Written by `python -m ml.data.crp manifest`; checked by `python -m ml.data.crp verify`.\n"
    "# source_url and downloaded_on are for the owner to fill; a rewrite keeps filled values.\n"
)


def file_facts(path: Path) -> dict[str, Any]:
    blob = path.read_bytes()
    if path.suffix.lower() == ".csv":
        lines = len(blob.decode("utf-8-sig").splitlines())
    else:
        lines = len(read_table(path))
    return {
        "name": path.name,
        "sha256": hashlib.sha256(blob).hexdigest(),
        "size_bytes": len(blob),
        "line_count": lines,
    }


def _load_manifest(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    files = _read_yaml(path).get("files") or []
    return {str(f["name"]): f for f in files}


def write_manifest(raw_dir: Path, manifest_path: Path, mapping: Mapping) -> None:
    old = _load_manifest(manifest_path)
    files = []
    for path in list_raw_files(raw_dir, mapping):
        facts = file_facts(path)
        for key in ("source_url", "downloaded_on"):
            facts[key] = old.get(path.name, {}).get(key, "PLACEHOLDER")
        files.append(facts)
    body = yaml.safe_dump({"version": 1, "files": files}, sort_keys=False)
    manifest_path.write_text(MANIFEST_HEADER + body, encoding="utf-8")


def verify_manifest(raw_dir: Path, manifest_path: Path, mapping: Mapping) -> list[str]:
    """Every difference between the raw folder and the manifest (empty list = match)."""
    if not manifest_path.exists():
        return [f"{manifest_path} does not exist"]
    expected = _load_manifest(manifest_path)
    actual = {p.name: file_facts(p) for p in list_raw_files(raw_dir, mapping)}
    problems = [
        f"{name}: in the manifest but not in {raw_dir}" for name in expected.keys() - actual.keys()
    ]
    problems += [
        f"{name}: in {raw_dir} but not in the manifest" for name in actual.keys() - expected.keys()
    ]
    for name in sorted(expected.keys() & actual.keys()):
        for key in ("sha256", "size_bytes", "line_count"):
            if expected[name].get(key) != actual[name][key]:
                problems.append(
                    f"{name}: {key} is {actual[name][key]}, manifest says {expected[name].get(key)}"
                )
    return sorted(problems)


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m ml.data.crp", description=__doc__.splitlines()[0])
    p.add_argument("command", choices=["inspect", "clean", "manifest", "verify"])
    p.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    p.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    p.add_argument("--qc", type=Path, default=DEFAULT_QC)
    p.add_argument("--corrections", type=Path, default=DEFAULT_CORRECTIONS)
    p.add_argument("--schema-doc", type=Path, default=DEFAULT_SCHEMA_DOC)
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    p.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        mapping = load_mapping(args.mapping)
        qc = load_qc(args.qc, mapping)
        corrections = load_corrections(args.corrections, mapping)
        if args.command == "manifest":
            write_manifest(args.raw_dir, args.manifest, mapping)
            print(f"wrote {args.manifest}")
            return 0
        if args.command == "verify":
            problems = verify_manifest(args.raw_dir, args.manifest, mapping)
            for problem in problems:
                print(f"MISMATCH: {problem}", file=sys.stderr)
            if not problems:
                print(f"OK: {args.raw_dir} matches {args.manifest}")
            return 1 if problems else 0
        res = run_pipeline(args.raw_dir, mapping, qc, corrections)
        if args.command == "inspect":
            args.schema_doc.write_text(
                render_schema(res, mapping, dt.datetime.now(dt.UTC).date()), encoding="utf-8"
            )
            print(f"wrote {args.schema_doc}")
            print(render_flags(res))
            return 0
        for path in write_clean(res, mapping, args.out_dir):
            print(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path}")
        print()
        print(label_table(res, long_records(res, mapping), mapping))
        return 0
    except CrpError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
