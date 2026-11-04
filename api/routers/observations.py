"""``GET /observations``: filtered, optionally resampled readings as columnar JSON."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any

import pandas as pd
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.sql import Select

from api.deps import SessionDep
from contract.enums import QUALITY_RANK, Kind, Quality, Source
from db.models import Observation

router = APIRouter(tags=["observations"])

MAX_ROWS = 200_000


class Resample(StrEnum):
    """Supported resample frequencies (pandas offset aliases)."""

    min15 = "15min"
    h1 = "1h"
    d1 = "1D"


class Agg(StrEnum):
    """Supported aggregations for resampled values."""

    mean = "mean"
    median = "median"
    last = "last"
    max = "max"
    min = "min"


class ObservationsOut(BaseModel):
    """Columnar response. Every list has ``count`` entries; ``n`` and ``meta`` depend on mode."""

    count: int
    resample: Resample | None
    agg: Agg | None
    ts: list[datetime]
    node_id: list[str]
    channel: list[str]
    value: list[float]
    unit: list[str]
    quality: list[str]
    kind: list[str]
    source: list[str]
    n: list[int] | None = None
    meta: list[dict[str, Any]] | None = None


def _aware(value: datetime | None, name: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{name} must be an aware timestamp (use 'Z' or an explicit offset)",
        )
    return value.astimezone(UTC)


def _apply_filters(
    stmt: Select,
    *,
    node_id: str | None,
    channel: str | None,
    kind: Kind | None,
    source: Source | None,
    start: datetime | None,
    end: datetime | None,
    min_quality: Quality | None,
) -> Select:
    if node_id:
        stmt = stmt.where(Observation.node_id == node_id)
    if channel:
        stmt = stmt.where(Observation.channel == channel)
    if kind:
        stmt = stmt.where(Observation.kind == str(kind))
    if source:
        stmt = stmt.where(Observation.source == str(source))
    if start:
        stmt = stmt.where(Observation.ts >= start)
    if end:
        stmt = stmt.where(Observation.ts <= end)
    if min_quality is not None and min_quality != Quality.bad:
        allowed = [str(q) for q, rank in QUALITY_RANK.items() if rank <= QUALITY_RANK[min_quality]]
        stmt = stmt.where(Observation.quality.in_(allowed))
    return stmt


QUALITY_RANK_STR = {str(q): r for q, r in QUALITY_RANK.items()}
RANK_TO_QUALITY = {r: str(q) for q, r in QUALITY_RANK.items()}
SERIES_KEYS = ["node_id", "channel", "kind", "source", "unit"]


def resample_frame(df: pd.DataFrame, freq: Resample, agg: Agg) -> pd.DataFrame:
    """Bucket rows per series by ``freq``; value by ``agg``, quality = worst in bucket, n = rows.

    Buckets are left-closed and left-labeled (``ts`` is the bucket start), matching
    ``DataFrame.resample(freq)`` defaults for these frequencies.
    """
    if df.empty:
        return pd.DataFrame(columns=[*SERIES_KEYS, "ts", "value", "quality", "n"])
    df = df.sort_values("ts")
    df["bucket"] = df["ts"].dt.floor(str(freq))
    df["qrank"] = df["quality"].map(QUALITY_RANK_STR)
    grouped = df.groupby([*SERIES_KEYS, "bucket"], sort=True)
    out = grouped.agg(
        value=("value", str(agg)),
        qrank=("qrank", "max"),
        n=("value", "size"),
    ).reset_index()
    out["quality"] = out["qrank"].map(RANK_TO_QUALITY)
    return out.drop(columns="qrank").rename(columns={"bucket": "ts"})


@router.get("/observations", response_model=ObservationsOut, response_model_exclude_none=True)
def get_observations(
    session: SessionDep,
    node_id: str | None = None,
    channel: str | None = None,
    kind: Kind | None = None,
    source: Source | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    min_quality: Annotated[
        Quality | None,
        Query(description="good = only good rows; suspect = good and suspect; bad = all (default)"),
    ] = None,
    resample: Resample | None = None,
    agg: Agg | None = None,
) -> ObservationsOut:
    """Readings as columnar JSON, optionally resampled.

    Raw mode returns one entry per row with ``meta``. With ``resample`` the rows are bucketed per
    (node, channel, kind, source): ``value`` is the ``agg`` (default mean) of the bucket,
    ``quality`` the worst quality in the bucket and ``n`` the number of rows. Resampling applies to
    ``measurement`` (default) or ``external`` rows, not to labels. Queries touching more than
    200,000 rows get 413: narrow ``start``/``end`` or filter by node and channel.
    """
    start = _aware(start, "start")
    end = _aware(end, "end")
    if resample is not None:
        if kind is None:
            kind = Kind.measurement
        if kind == Kind.label:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="labels cannot be resampled; request kind=label without resample",
            )
        agg = agg or Agg.mean
    elif agg is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="agg requires resample")
    filters = dict(
        node_id=node_id,
        channel=channel,
        kind=kind,
        source=source,
        start=start,
        end=end,
        min_quality=min_quality,
    )
    n_rows = session.scalar(
        _apply_filters(select(func.count()).select_from(Observation), **filters)
    )
    if n_rows is not None and n_rows > MAX_ROWS:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=(
                f"query matches {n_rows} rows, above the limit of {MAX_ROWS}. Narrow start/end, "
                "filter by node_id and channel, or (for raw queries) add resample=1h&agg=mean."
            ),
        )
    stmt = _apply_filters(
        select(
            Observation.ts,
            Observation.node_id,
            Observation.channel,
            Observation.value,
            Observation.unit,
            Observation.quality,
            Observation.kind,
            Observation.source,
            Observation.meta_json,
        ),
        **filters,
    ).order_by(Observation.ts, Observation.node_id, Observation.channel)
    rows = session.execute(stmt).all()

    if resample is None:
        return ObservationsOut(
            count=len(rows),
            resample=None,
            agg=None,
            ts=[r.ts for r in rows],
            node_id=[r.node_id for r in rows],
            channel=[r.channel for r in rows],
            value=[r.value for r in rows],
            unit=[r.unit for r in rows],
            quality=[r.quality for r in rows],
            kind=[r.kind for r in rows],
            source=[r.source for r in rows],
            meta=[json.loads(r.meta_json) if r.meta_json else {} for r in rows],
        )

    df = pd.DataFrame(
        {
            "ts": pd.to_datetime([r.ts for r in rows], utc=True),
            "node_id": [r.node_id for r in rows],
            "channel": [r.channel for r in rows],
            "value": [r.value for r in rows],
            "unit": [r.unit for r in rows],
            "quality": [r.quality for r in rows],
            "kind": [r.kind for r in rows],
            "source": [r.source for r in rows],
        }
    )
    out = resample_frame(df, resample, agg or Agg.mean)
    out = out.sort_values(["ts", "node_id", "channel"]) if not out.empty else out
    return ObservationsOut(
        count=len(out),
        resample=resample,
        agg=agg,
        ts=[t.to_pydatetime() for t in out["ts"]],
        node_id=out["node_id"].tolist(),
        channel=out["channel"].tolist(),
        value=[float(v) for v in out["value"]],
        unit=out["unit"].tolist(),
        quality=out["quality"].tolist(),
        kind=out["kind"].tolist(),
        source=out["source"].tolist(),
        n=[int(v) for v in out["n"]],
    )
