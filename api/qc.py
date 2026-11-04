"""Server-side quality control driven by ``contract/channels.yaml`` (CLAUDE.md rule 6).

QC can only downgrade quality (good -> suspect -> bad), never upgrade what the node sent. Each
check yields a reason from the conventional list: ``out_of_range``, ``saturated``, ``spike``,
``stuck``. The first check that fires wins; the value itself is always kept.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from contract.enums import Kind, Quality, worst_quality
from contract.models import Reading
from contract.registry import ChannelSpec

STEP_WINDOW_MIN = 15.0
HISTORY_ROWS = 24  # earlier stored rows per (node, channel) loaded for spike and stuck checks


@dataclass(frozen=True, slots=True)
class Point:
    """A stored or in-batch reading reduced to what QC needs."""

    ts: datetime
    value: float
    quality: Quality


@dataclass(slots=True)
class QCOutcome:
    """Result of QC for one reading."""

    quality: Quality
    reason: str | None  # set only when QC downgraded the reading


def _range_reason(spec: ChannelSpec, value: float) -> str | None:
    if value < spec.valid_min or value > spec.valid_max:
        return "out_of_range"
    if spec.saturation_min is not None and value >= spec.saturation_min:
        return "saturated"
    return None


def _spike_reason(spec: ChannelSpec, point: Point, earlier: list[Point]) -> str | None:
    if spec.max_step_15min is None:
        return None
    # Compare against the nearest earlier reading that QC and the node both consider good, so a
    # single spike is flagged once and the return to normal is not flagged as a second spike.
    ref = next((p for p in reversed(earlier) if p.quality == Quality.good), None)
    if ref is None:
        return None
    gap_min = (point.ts - ref.ts).total_seconds() / 60.0
    allowed = spec.max_step_15min * max(gap_min / STEP_WINDOW_MIN, 1.0)
    if abs(point.value - ref.value) > allowed:
        return "spike"
    return None


def _stuck_reason(spec: ChannelSpec, point: Point, earlier: list[Point]) -> str | None:
    if spec.stuck_after is None:
        return None
    run = 1
    for p in reversed(earlier):
        if p.value != point.value:
            break
        run += 1
        if run >= spec.stuck_after:
            return "stuck"
    return None


def check_reading(spec: ChannelSpec, reading: Reading, earlier: list[Point]) -> QCOutcome:
    """Run every QC check for one reading against the readings strictly before it.

    ``earlier`` holds the nearest earlier points of the same node and channel (stored rows and
    earlier readings of the same batch), sorted by ``ts`` ascending.
    """
    sent = reading.quality
    point = Point(reading.ts, reading.value, sent)
    reason = _range_reason(spec, reading.value)
    if reason is None and reading.kind == Kind.measurement:
        reason = _spike_reason(spec, point, earlier)
    if reason is None and reading.kind == Kind.measurement:
        reason = _stuck_reason(spec, point, earlier)
    if reason is None:
        return QCOutcome(quality=sent, reason=None)
    return QCOutcome(quality=worst_quality(sent, Quality.suspect), reason=reason)
