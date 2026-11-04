"""``GET /reach``: the reach configuration (nodes in river order, effluent input)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from db.seed import load_reach

router = APIRouter(tags=["reach"])


@router.get("/reach")
def get_reach() -> dict[str, Any]:
    """Return ``config/reach.yaml`` as JSON. Every location in it is a PLACEHOLDER."""
    return load_reach()
