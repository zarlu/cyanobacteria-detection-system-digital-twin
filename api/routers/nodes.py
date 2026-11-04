"""``GET /nodes``: nodes with freshness and quality counts for the last hour."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select

from api.deps import SessionDep
from db.models import Node, Observation

router = APIRouter(tags=["nodes"])

WINDOW = timedelta(hours=1)


class NodeStatus(BaseModel):
    """One node with its freshness and last-hour quality counts."""

    node_id: str
    name: str | None
    role: str
    lat: float | None
    lon: float | None
    river_km: float | None
    notes: str | None
    last_seen: datetime | None
    minutes_since_seen: float | None
    last_received_at: datetime | None
    n_rows: int
    good_1h: int
    suspect_1h: int
    bad_1h: int


@router.get("/nodes", response_model=list[NodeStatus])
def list_nodes(session: SessionDep) -> list[NodeStatus]:
    """Every node with ``last_seen`` (newest reading timestamp), minutes silent and quality counts.

    ``last_seen`` is the newest ``ts`` the node has data for; ``last_received_at`` is when the API
    last heard from it (they differ after a buffered late upload).
    """
    now = datetime.now(UTC)
    nodes = session.scalars(select(Node).order_by(Node.river_km.is_(None), Node.river_km)).all()
    agg = session.execute(
        select(
            Observation.node_id,
            func.max(Observation.ts),
            func.max(Observation.received_at),
            func.count(),
        ).group_by(Observation.node_id)
    ).all()
    freshness = {node_id: (ts, rcv, n) for node_id, ts, rcv, n in agg}
    counts = session.execute(
        select(Observation.node_id, Observation.quality, func.count())
        .where(Observation.ts >= now - WINDOW)
        .group_by(Observation.node_id, Observation.quality)
    ).all()
    by_quality: dict[str, dict[str, int]] = {}
    for node_id, quality, n in counts:
        by_quality.setdefault(node_id, {})[quality] = n
    out: list[NodeStatus] = []
    for node in nodes:
        last_ts, last_rcv, n_rows = freshness.get(node.node_id, (None, None, 0))
        last_ts = last_ts.replace(tzinfo=UTC) if last_ts and last_ts.tzinfo is None else last_ts
        last_rcv = (
            last_rcv.replace(tzinfo=UTC) if last_rcv and last_rcv.tzinfo is None else last_rcv
        )
        q = by_quality.get(node.node_id, {})
        out.append(
            NodeStatus(
                node_id=node.node_id,
                name=node.name,
                role=node.role,
                lat=node.lat,
                lon=node.lon,
                river_km=node.river_km,
                notes=node.notes,
                last_seen=last_ts,
                minutes_since_seen=(
                    round((now - last_ts).total_seconds() / 60.0, 1) if last_ts else None
                ),
                last_received_at=last_rcv,
                n_rows=n_rows,
                good_1h=q.get("good", 0),
                suspect_1h=q.get("suspect", 0),
                bad_1h=q.get("bad", 0),
            )
        )
    return out
