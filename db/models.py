"""SQLAlchemy 2.0 models (CLAUDE.md section 6). Schema changes go through ``make reset-db``."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from db.types import UTCDateTime

NODE_ROLES = ("intake", "downstream", "effluent_input", "bench", "unknown")
MODEL_STATUSES = ("candidate", "champion", "retired")


class Base(DeclarativeBase):
    """Declarative base for every table."""


class Node(Base):
    """A measurement site (or the lab bench)."""

    __tablename__ = "nodes"

    node_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    river_km: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)


class Channel(Base):
    """Channel registry row, seeded from ``contract/channels.yaml``."""

    __tablename__ = "channels"

    channel: Mapped[str] = mapped_column(String(64), primary_key=True)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    default_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    valid_min: Mapped[float | None] = mapped_column(Float)
    valid_max: Mapped[float | None] = mapped_column(Float)
    max_step_15min: Mapped[float | None] = mapped_column(Float)


class Observation(Base):
    """One stored reading. Uniqueness key ``(node_id, channel, ts, kind, source)``."""

    __tablename__ = "observations"
    __table_args__ = (
        UniqueConstraint("node_id", "channel", "ts", "kind", "source", name="uq_observation_key"),
        Index("ix_observations_node_channel_ts", "node_id", "channel", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    node_id: Mapped[str] = mapped_column(String(64), ForeignKey("nodes.node_id"), nullable=False)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    quality: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    meta_json: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class Model(Base):
    """Model registry (owned by B from Phase 4; table created here so the schema is complete)."""

    __tablename__ = "models"

    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    task: Mapped[str] = mapped_column(String(64), nullable=False)
    algorithm: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(64), nullable=False)
    horizon_h: Mapped[float | None] = mapped_column(Float)
    features_json: Mapped[str | None] = mapped_column(Text)
    train_start: Mapped[datetime | None] = mapped_column(UTCDateTime)
    train_end: Mapped[datetime | None] = mapped_column(UTCDateTime)
    train_sources: Mapped[str | None] = mapped_column(String(200))
    n_train: Mapped[int | None] = mapped_column(Integer)
    metrics_json: Mapped[str | None] = mapped_column(Text)
    baseline_metrics_json: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="candidate")
    artifact_path: Mapped[str | None] = mapped_column(String(400))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class Prediction(Base):
    """Stored prediction; the persistence baseline uses ``model_version='persistence-v1'``."""

    __tablename__ = "predictions"
    __table_args__ = (
        UniqueConstraint(
            "model_version", "node_id", "target", "issued_at", "horizon_h", name="uq_prediction_key"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    issued_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    valid_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    horizon_h: Mapped[float] = mapped_column(Float, nullable=False)
    node_id: Mapped[str] = mapped_column(String(64), ForeignKey("nodes.node_id"), nullable=False)
    target: Mapped[str] = mapped_column(String(64), nullable=False)
    yhat: Mapped[float | None] = mapped_column(Float)
    lo: Mapped[float | None] = mapped_column(Float)
    hi: Mapped[float | None] = mapped_column(Float)
    p_event: Mapped[float | None] = mapped_column(Float)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)


class Alert(Base):
    """Alert raised by the rule engine (alerts phase)."""

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    node_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("nodes.node_id"))
    rule_id: Mapped[str] = mapped_column(String(64), nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    p_event: Mapped[float | None] = mapped_column(Float)
    model_version: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
