"""Read observations from the database in the long layout of ``ml.source.build_frame``.

The demo loop trains and forecasts from what ``/ingest`` stored, not from the clean CSV. A node
maps to a station by removing the task's ``deploy.node_prefix`` from the node id.
Quality is the stored (server QC) quality; censoring comes from ``meta``.
"""

from __future__ import annotations

import json

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Observation

COLUMNS = [
    "ts_utc",
    "station",
    "node_id",
    "channel",
    "value",
    "unit",
    "kind",
    "source",
    "quality",
    "censored",
    "detection_limit",
]


def read_long(
    session: Session,
    source: str,
    node_prefix: str,
    channels: list[str] | None = None,
    node_id: str | None = None,
) -> pd.DataFrame:
    """Stored rows of one ``source`` whose node id starts with ``node_prefix``."""
    stmt = select(
        Observation.ts,
        Observation.node_id,
        Observation.channel,
        Observation.value,
        Observation.unit,
        Observation.kind,
        Observation.source,
        Observation.quality,
        Observation.meta_json,
    ).where(Observation.source == source, Observation.node_id.like(f"{node_prefix}%"))
    if channels:
        stmt = stmt.where(Observation.channel.in_(channels))
    if node_id:
        stmt = stmt.where(Observation.node_id == node_id)
    rows = session.execute(stmt.order_by(Observation.ts, Observation.node_id)).all()
    records = []
    for r in rows:
        meta = json.loads(r.meta_json) if r.meta_json else {}
        records.append(
            {
                "ts_utc": pd.Timestamp(r.ts).tz_localize("UTC")
                if r.ts.tzinfo is None
                else pd.Timestamp(r.ts).tz_convert("UTC"),
                "station": r.node_id[len(node_prefix) :],
                "node_id": r.node_id,
                "channel": r.channel,
                "value": r.value,
                "unit": r.unit,
                "kind": r.kind,
                "source": r.source,
                "quality": r.quality,
                "censored": meta.get("censored", "none"),
                "detection_limit": meta.get("detection_limit"),
            }
        )
    out = pd.DataFrame.from_records(records, columns=COLUMNS)
    out["detection_limit"] = out["detection_limit"].astype(float)
    return out
