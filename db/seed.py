"""Seed the ``channels`` and ``nodes`` tables from YAML (idempotent upserts)."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from sqlalchemy.orm import Session

from contract.registry import CHANNELS_YAML, load_registry
from db.models import NODE_ROLES, Channel, Node

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
REACH_YAML = REPO_ROOT / "config" / "reach.yaml"


def seed_channels(session: Session, path: Path | None = None) -> int:
    """Upsert every channel from ``contract/channels.yaml``. Returns the number of channels."""
    registry = load_registry(path) if path else load_registry()
    for spec in registry.values():
        row = session.get(Channel, spec.channel)
        if row is None:
            row = Channel(channel=spec.channel)
            session.add(row)
        row.unit = spec.unit
        row.description = spec.description
        row.default_kind = str(spec.default_kind)
        row.valid_min = spec.valid_min
        row.valid_max = spec.valid_max
        row.max_step_15min = spec.max_step_15min
    session.commit()
    log.info("seeded %d channels from %s", len(registry), path or CHANNELS_YAML)
    return len(registry)


def load_reach(path: Path | None = None) -> dict:
    """Load ``config/reach.yaml``."""
    return yaml.safe_load((path or REACH_YAML).read_text(encoding="utf-8"))


def seed_reach(session: Session, path: Path | None = None) -> int:
    """Upsert the nodes of ``config/reach.yaml``. Returns the number of nodes."""
    reach = load_reach(path)
    nodes = reach.get("nodes", [])
    for entry in nodes:
        role = entry.get("role", "unknown")
        if role not in NODE_ROLES:
            raise ValueError(
                f"node {entry['node_id']!r} has unknown role {role!r}; allowed: {NODE_ROLES}"
            )
        row = session.get(Node, entry["node_id"])
        if row is None:
            row = Node(node_id=entry["node_id"])
            session.add(row)
        row.name = entry.get("name")
        row.role = role
        row.lat = entry.get("lat")
        row.lon = entry.get("lon")
        row.river_km = entry.get("river_km")
        row.notes = entry.get("notes")
    session.commit()
    log.info("seeded %d nodes from %s", len(nodes), path or REACH_YAML)
    return len(nodes)


def seed_all(session: Session) -> None:
    """Seed channels and reach nodes."""
    seed_channels(session)
    seed_reach(session)
