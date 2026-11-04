"""Ingest service: per-reading validation, QC and idempotent insert.

Used by ``POST /ingest`` and ``POST /labels``. The API routers only translate HTTP to this call.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from api.qc import HISTORY_ROWS, Point, check_reading
from contract.enums import Quality, Source
from contract.models import Reading
from contract.registry import load_registry
from db.models import Node, Observation

log = logging.getLogger(__name__)

INSERT_CHUNK = 500  # rows per INSERT statement (SQLite variable limit is 32766)


class Rejection(BaseModel):
    """One rejected reading."""

    index: int
    reason: str


class IngestResult(BaseModel):
    """Response of ``POST /ingest``."""

    accepted: int = 0
    duplicates: int = 0
    rejected: list[Rejection] = []


@dataclass
class _Validated:
    index: int
    reading: Reading
    quality: Quality = Quality.good
    meta: dict[str, Any] = field(default_factory=dict)


def _first_error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    loc = ".".join(str(p) for p in err.get("loc", ()))
    msg = err.get("msg", "invalid")
    if msg.startswith("Value error, "):
        msg = msg[len("Value error, ") :]
    return f"{loc}: {msg}" if loc else msg


def validate_readings(
    raw_readings: list[Any], node_id: str, message_source: Source
) -> tuple[list[_Validated], list[Rejection]]:
    """Validate each raw reading on its own; reject in-batch duplicates (first occurrence wins)."""
    valid: list[_Validated] = []
    rejected: list[Rejection] = []
    seen: set[tuple[str, str, datetime, str, str]] = set()
    for i, raw in enumerate(raw_readings):
        try:
            reading = Reading.model_validate(raw, context={"source": str(message_source)})
        except ValidationError as exc:
            rejected.append(Rejection(index=i, reason=_first_error(exc)))
            continue
        key = reading.dedup_key(node_id, message_source)
        if key in seen:
            rejected.append(
                Rejection(index=i, reason="duplicate_in_batch: same (channel, ts, kind, source)")
            )
            continue
        seen.add(key)
        valid.append(_Validated(index=i, reading=reading, quality=reading.quality))
    return valid, rejected


def ensure_node(session: Session, node_id: str) -> None:
    """Create ``node_id`` with role ``unknown`` when it does not exist (and log it)."""
    if session.get(Node, node_id) is None:
        session.add(
            Node(node_id=node_id, role="unknown", name=node_id, notes="auto-created by /ingest")
        )
        session.flush()
        log.warning("unknown node_id %r auto-created with role 'unknown'", node_id)


def _history(
    session: Session, node_id: str, channel: str, lo: datetime, hi: datetime
) -> list[Point]:
    """Stored points for (node, channel): last HISTORY_ROWS before ``lo`` plus all in [lo, hi]."""
    before = session.execute(
        select(Observation.ts, Observation.value, Observation.quality)
        .where(Observation.node_id == node_id, Observation.channel == channel, Observation.ts < lo)
        .order_by(Observation.ts.desc())
        .limit(HISTORY_ROWS)
    ).all()
    within = session.execute(
        select(Observation.ts, Observation.value, Observation.quality)
        .where(
            Observation.node_id == node_id,
            Observation.channel == channel,
            Observation.ts >= lo,
            Observation.ts <= hi,
        )
        .order_by(Observation.ts)
    ).all()
    return [
        Point(ts, value, Quality(quality))
        for ts, value, quality in list(reversed(before)) + list(within)
    ]


def apply_qc(session: Session, node_id: str, valid: list[_Validated]) -> None:
    """Downgrade quality in place per ``api.qc``; compares with the nearest earlier readings."""
    registry = load_registry()
    by_channel: dict[str, list[_Validated]] = defaultdict(list)
    for v in valid:
        by_channel[v.reading.channel].append(v)
    for channel, items in by_channel.items():
        spec = registry[channel]
        items.sort(key=lambda v: v.reading.ts)
        timeline = _history(session, node_id, channel, items[0].reading.ts, items[-1].reading.ts)
        for v in items:
            earlier = [p for p in timeline if p.ts < v.reading.ts]
            outcome = check_reading(spec, v.reading, earlier)
            v.quality = outcome.quality
            meta = dict(v.reading.meta)
            if outcome.reason is not None:
                if "reason" in meta and meta["reason"] != outcome.reason:
                    meta["qc_reason"] = outcome.reason
                else:
                    meta["reason"] = outcome.reason
            v.meta = meta
            timeline.append(Point(v.reading.ts, v.reading.value, v.quality))
            timeline.sort(key=lambda p: p.ts)


def insert_observations(
    session: Session,
    node_id: str,
    message_source: Source,
    valid: list[_Validated],
    received_at: datetime,
) -> tuple[int, int]:
    """``INSERT ... ON CONFLICT DO NOTHING`` in chunks; duplicates come from affected-row counts."""
    rows = [
        {
            "ts": v.reading.ts,
            "node_id": node_id,
            "channel": v.reading.channel,
            "value": v.reading.value,
            "unit": v.reading.unit,
            "quality": str(v.quality),
            "kind": str(v.reading.kind),
            "source": str(v.reading.source or message_source),
            "meta_json": json.dumps(v.meta, sort_keys=True, separators=(",", ":")),
            "received_at": received_at,
        }
        for v in valid
    ]
    inserted = 0
    for start in range(0, len(rows), INSERT_CHUNK):
        chunk = rows[start : start + INSERT_CHUNK]
        stmt = (
            sqlite_insert(Observation)
            .values(chunk)
            .on_conflict_do_nothing(index_elements=["node_id", "channel", "ts", "kind", "source"])
        )
        inserted += session.execute(stmt).rowcount
    return inserted, len(rows) - inserted


def ingest_readings(
    session: Session,
    node_id: str,
    message_source: Source,
    raw_readings: list[Any],
    received_at: datetime | None = None,
) -> IngestResult:
    """Validate, QC and store a batch of readings for one node. Commits on success."""
    received_at = received_at or datetime.now(UTC)
    valid, rejected = validate_readings(raw_readings, node_id, message_source)
    if not valid:
        return IngestResult(accepted=0, duplicates=0, rejected=rejected)
    ensure_node(session, node_id)
    apply_qc(session, node_id, valid)
    accepted, duplicates = insert_observations(session, node_id, message_source, valid, received_at)
    session.commit()
    log.info(
        "ingest node=%s accepted=%d duplicates=%d rejected=%d",
        node_id,
        accepted,
        duplicates,
        len(rejected),
    )
    return IngestResult(accepted=accepted, duplicates=duplicates, rejected=rejected)
