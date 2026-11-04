"""``GET /channels``: the channel registry as seeded in the database."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from api.deps import SessionDep
from contract.registry import load_registry
from db.models import Channel

router = APIRouter(tags=["channels"])


class ChannelOut(BaseModel):
    """Channel registry row plus the YAML-only ``stuck_after``."""

    channel: str
    unit: str
    description: str | None
    default_kind: str
    valid_min: float | None
    valid_max: float | None
    max_step_15min: float | None
    stuck_after: int | None


@router.get("/channels", response_model=list[ChannelOut])
def list_channels(session: SessionDep) -> list[ChannelOut]:
    """Channel registry: units, default kind and QC limits."""
    registry = load_registry()
    rows = session.scalars(select(Channel).order_by(Channel.channel)).all()
    return [
        ChannelOut(
            channel=r.channel,
            unit=r.unit,
            description=r.description,
            default_kind=r.default_kind,
            valid_min=r.valid_min,
            valid_max=r.valid_max,
            max_step_15min=r.max_step_15min,
            stuck_after=registry[r.channel].stuck_after if r.channel in registry else None,
        )
        for r in rows
    ]
