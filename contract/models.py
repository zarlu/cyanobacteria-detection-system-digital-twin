"""Pydantic v2 models for the ingest message (contract v1, CLAUDE.md section 5).

``IngestMessage`` validates a whole message strictly (used for the contract examples and by
clients). ``IngestEnvelope`` validates only the envelope and leaves ``readings`` raw so the API can
validate each reading on its own and store the valid ones.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from contract.enums import Kind, Quality, Source
from contract.registry import get_channel

SCHEMA_VERSION = 1
MAX_FUTURE = timedelta(minutes=10)
NODE_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"

NodeId = Annotated[str, Field(pattern=NODE_ID_PATTERN, description="lowercase slug")]


def normalize_utc(value: datetime, *, field: str) -> datetime:
    """Require an aware timestamp, convert to UTC and truncate to whole seconds."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} is a naive timestamp; send UTC with 'Z' or an explicit offset")
    return value.astimezone(UTC).replace(microsecond=0)


def reject_far_future(value: datetime, *, field: str, now: datetime | None = None) -> datetime:
    """Reject timestamps more than ``MAX_FUTURE`` ahead of now."""
    now = now or datetime.now(UTC)
    if value > now + MAX_FUTURE:
        raise ValueError(
            f"{field} {value.isoformat()} is more than 10 minutes in the future "
            f"(server UTC now {now:%Y-%m-%dT%H:%M:%SZ})"
        )
    return value


class Reading(BaseModel):
    """One reading inside an ingest message."""

    model_config = ConfigDict(extra="forbid")

    ts: datetime
    channel: str
    value: float
    unit: str
    quality: Quality = Quality.good
    kind: Kind | None = None
    source: Source | None = None
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("ts")
    @classmethod
    def _ts_utc(cls, v: datetime) -> datetime:
        return reject_far_future(normalize_utc(v, field="ts"), field="ts")

    @field_validator("value", mode="before")
    @classmethod
    def _finite(cls, v: Any) -> Any:
        if isinstance(v, bool):
            raise ValueError("value must be a number, not a boolean")
        if isinstance(v, str):
            raise ValueError("value must be a number, not a string")
        if isinstance(v, int | float) and not math.isfinite(float(v)):
            raise ValueError("value must be a finite number (NaN and infinities are rejected)")
        return v

    @model_validator(mode="after")
    def _channel_and_unit(self, info: ValidationInfo) -> Reading:
        spec = get_channel(self.channel)
        if spec is None:
            raise ValueError(f"unknown channel '{self.channel}' (see contract/channels.yaml)")
        if self.unit != spec.unit:
            raise ValueError(
                f"unit '{self.unit}' for channel '{self.channel}' must be '{spec.unit}'"
            )
        if self.kind is None:
            self.kind = spec.default_kind
        if self.source is None:
            ctx = info.context or {}
            default_source = ctx.get("source")
            if default_source is not None:
                self.source = Source(default_source)
        return self

    def dedup_key(
        self, node_id: str, message_source: Source
    ) -> tuple[str, str, datetime, str, str]:
        """The uniqueness key ``(node_id, channel, ts, kind, source)``."""
        kind = self.kind or Kind.measurement
        source = self.source or message_source
        return (node_id, self.channel, self.ts, str(kind), str(source))


class IngestEnvelope(BaseModel):
    """The message envelope with ``readings`` left unvalidated (for per-reading validation)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    node_id: NodeId
    source: Source
    sent_at: datetime
    firmware: str | None = None
    readings: list[Any] = Field(min_length=1)

    @field_validator("sent_at")
    @classmethod
    def _sent_at_utc(cls, v: datetime) -> datetime:
        return normalize_utc(v, field="sent_at")


class IngestMessage(IngestEnvelope):
    """A fully validated ingest message: every reading valid, no duplicate key inside the batch."""

    readings: list[Reading] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _inject_default_source(cls, data: Any) -> Any:
        # Readings inherit the message source when they do not set their own.
        if isinstance(data, dict) and isinstance(data.get("readings"), list):
            src = data.get("source")
            if src is not None:
                readings = []
                for r in data["readings"]:
                    if isinstance(r, dict) and r.get("source") is None:
                        r = {**r, "source": src}
                    readings.append(r)
                data = {**data, "readings": readings}
        return data

    @model_validator(mode="after")
    def _no_duplicate_keys(self) -> IngestMessage:
        seen: set[tuple[str, str, datetime, str, str]] = set()
        for i, r in enumerate(self.readings):
            key = r.dedup_key(self.node_id, self.source)
            if key in seen:
                raise ValueError(
                    f"readings[{i}] repeats the key (node_id, channel, ts, kind, source) of an "
                    f"earlier reading in the same batch: {key[1]} @ {key[2].isoformat()}"
                )
            seen.add(key)
        return self
