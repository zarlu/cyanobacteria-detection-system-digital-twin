"""Unique constraint, registry/seed agreement, idempotent seeding, WAL."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from contract import load_registry
from db import Channel, Node, Observation, seed_channels, seed_reach


def _obs(**kw: object) -> Observation:
    base = dict(
        ts=datetime(2026, 10, 10, 15, 4, tzinfo=UTC),
        node_id="node-intake",
        channel="water_temp",
        value=20.0,
        unit="degC",
        quality="good",
        kind="measurement",
        source="sensor",
        meta_json="{}",
        received_at=datetime.now(UTC),
    )
    base.update(kw)
    return Observation(**base)


def test_unique_constraint_rejects_duplicate(session: Session) -> None:
    seed_reach(session)
    session.add(_obs())
    session.commit()
    session.add(_obs(value=21.0))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    # A different source with the same ts is a different key.
    session.add(_obs(source="simulator"))
    session.commit()
    assert session.scalar(select(func.count()).select_from(Observation)) == 2


def test_naive_datetime_is_refused_at_db_boundary(session: Session) -> None:
    seed_reach(session)
    session.add(_obs(ts=datetime(2026, 10, 10, 15, 4)))
    with pytest.raises(Exception, match="naive"):
        session.commit()
    session.rollback()


def test_stored_timestamps_come_back_utc_aware(session: Session) -> None:
    seed_reach(session)
    session.add(_obs())
    session.commit()
    session.expire_all()
    row = session.scalars(select(Observation)).one()
    assert row.ts == datetime(2026, 10, 10, 15, 4, tzinfo=UTC)
    assert row.ts.tzinfo is not None


def test_channels_yaml_and_seeded_table_agree(session: Session) -> None:
    n = seed_channels(session)
    registry = load_registry()
    rows = {c.channel: c for c in session.scalars(select(Channel)).all()}
    assert n == len(registry) == len(rows)
    for name, spec in registry.items():
        row = rows[name]
        assert row.unit == spec.unit
        assert row.default_kind == str(spec.default_kind)
        assert row.valid_min == spec.valid_min
        assert row.valid_max == spec.valid_max
        assert row.max_step_15min == spec.max_step_15min


def test_seeding_is_idempotent(session: Session) -> None:
    seed_channels(session)
    seed_reach(session)
    first_nodes = session.scalar(select(func.count()).select_from(Node))
    first_channels = session.scalar(select(func.count()).select_from(Channel))
    seed_channels(session)
    seed_reach(session)
    assert session.scalar(select(func.count()).select_from(Node)) == first_nodes
    assert session.scalar(select(func.count()).select_from(Channel)) == first_channels
    roles = {n.node_id: n.role for n in session.scalars(select(Node))}
    assert roles == {
        "node-intake": "intake",
        "effluent-zacate": "effluent_input",
        "node-downstream": "downstream",
        "node-bench": "bench",
    }


def test_sqlite_uses_wal(engine: Engine) -> None:
    with engine.connect() as conn:
        mode = conn.execute(text("PRAGMA journal_mode")).scalar()
    assert mode == "wal"
