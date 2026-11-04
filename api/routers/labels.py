"""``POST /labels`` (one label) and ``POST /labels/csv`` (bulk grab samples)."""

from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

from api.deps import SessionDep, require_api_key
from api.ingest import IngestResult, ingest_readings
from contract.enums import Quality, Source
from contract.models import NODE_ID_PATTERN, NodeId
from contract.registry import get_channel

router = APIRouter(tags=["labels"], dependencies=[Depends(require_api_key)])

DEFAULT_LABEL_CHANNEL = "phosphate_po4"
DEFAULT_LABEL_SOURCE = Source.HI713
CSV_COLUMNS = ("ts_utc", "node_id", "channel", "value", "unit", "source", "sample_id", "notes")
CSV_REQUIRED = ("ts_utc", "node_id", "value")


class LabelIn(BaseModel):
    """One lab or grab-sample label."""

    ts: datetime
    node_id: NodeId
    channel: str = DEFAULT_LABEL_CHANNEL
    value: float
    unit: str | None = Field(default=None, description="defaults to the channel's canonical unit")
    source: Source = DEFAULT_LABEL_SOURCE
    quality: Quality = Quality.good
    sample_id: str | None = None
    notes: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict, description="extra meta keys, e.g. emulates")


class LineRejection(BaseModel):
    """One rejected CSV line (1-based line number in the file; line 1 is the header)."""

    line: int
    reason: str


class CsvLabelResult(BaseModel):
    """Response of ``POST /labels/csv``."""

    accepted: int = 0
    duplicates: int = 0
    rejected: list[LineRejection] = []


def _reading_dict(
    ts: Any,
    channel: str,
    value: Any,
    unit: str | None,
    source: str,
    meta: dict[str, Any],
    quality: str,
) -> dict[str, Any]:
    spec = get_channel(channel)
    return {
        "ts": ts,
        "channel": channel,
        "value": value,
        "unit": unit or (spec.unit if spec else ""),
        "kind": "label",
        "source": source,
        "quality": quality,
        "meta": {k: v for k, v in meta.items() if v not in (None, "")},
    }


@router.post("/labels", response_model=IngestResult)
def post_label(label: LabelIn, session: SessionDep) -> IngestResult:
    """Store one label (kind ``label``). Same validation, QC and idempotency as ``/ingest``."""
    reading = _reading_dict(
        label.ts,
        label.channel,
        label.value,
        label.unit,
        str(label.source),
        {**label.meta, "sample_id": label.sample_id, "notes": label.notes},
        str(label.quality),
    )
    return ingest_readings(session, label.node_id, label.source, [reading])


@router.post("/labels/csv", response_model=CsvLabelResult)
async def post_labels_csv(session: SessionDep, file: UploadFile = File(...)) -> CsvLabelResult:
    """Bulk labels from a CSV: ``ts_utc,node_id,channel,value,unit,source,sample_id,notes``.

    Blank ``channel`` defaults to ``phosphate_po4``, blank ``source`` to ``HI713``, blank ``unit``
    to the channel's canonical unit. Errors are reported per line (line 1 is the header).
    """
    text = (await file.read()).decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip() for h in (reader.fieldnames or [])]
    missing = [c for c in CSV_REQUIRED if c not in header]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"CSV header is missing required columns {missing}; "
            f"expected {list(CSV_COLUMNS)}",
        )
    rejected: list[LineRejection] = []
    groups: dict[tuple[str, str], list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for line_no, row in enumerate(reader, start=2):
        row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        node_id = row.get("node_id", "")
        source = row.get("source") or str(DEFAULT_LABEL_SOURCE)
        if source not in Source.__members__:
            rejected.append(
                LineRejection(line=line_no, reason=f"source: unknown source '{source}'")
            )
            continue
        if not re.fullmatch(NODE_ID_PATTERN, node_id):
            rejected.append(
                LineRejection(
                    line=line_no, reason=f"node_id: '{node_id}' must match {NODE_ID_PATTERN}"
                )
            )
            continue
        try:
            value: Any = float(row.get("value", ""))
        except ValueError:
            rejected.append(
                LineRejection(
                    line=line_no, reason=f"value: '{row.get('value', '')}' is not a number"
                )
            )
            continue
        reading = _reading_dict(
            row.get("ts_utc", ""),
            row.get("channel") or DEFAULT_LABEL_CHANNEL,
            value,
            row.get("unit") or None,
            source,
            {"sample_id": row.get("sample_id"), "notes": row.get("notes")},
            "good",
        )
        groups[(node_id, source)].append((line_no, reading))

    accepted = duplicates = 0
    for (node_id, source), items in groups.items():
        result = ingest_readings(session, node_id, Source(source), [r for _, r in items])
        accepted += result.accepted
        duplicates += result.duplicates
        for rej in result.rejected:
            rejected.append(LineRejection(line=items[rej.index][0], reason=rej.reason))
    rejected.sort(key=lambda r: r.line)
    return CsvLabelResult(accepted=accepted, duplicates=duplicates, rejected=rejected)
