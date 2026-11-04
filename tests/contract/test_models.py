"""Timestamp normalization and defaults of the contract models."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from contract import Kind, Reading, Source
from contract.enums import Quality, worst_quality


def _reading(**overrides: object) -> dict:
    base = {
        "ts": "2026-10-10T15:04:00.750Z",
        "channel": "water_temp",
        "value": 20.0,
        "unit": "degC",
    }
    return {**base, **overrides}


def test_offset_is_normalized_to_utc_and_truncated() -> None:
    r = Reading.model_validate(_reading(ts="2026-10-10T09:04:00.999-06:00"))
    assert r.ts == datetime(2026, 10, 10, 15, 4, 0, tzinfo=UTC)
    assert r.ts.microsecond == 0


def test_defaults_come_from_registry_and_context() -> None:
    r = Reading.model_validate(_reading(), context={"source": "sensor"})
    assert r.kind == Kind.measurement
    assert r.source == Source.sensor
    label = Reading.model_validate(_reading(channel="chlorophyll_a", unit="ug/L"))
    assert label.kind == Kind.label
    assert label.source is None


def test_future_within_ten_minutes_is_accepted() -> None:
    soon = datetime.now(UTC) + timedelta(minutes=9)
    Reading.model_validate(_reading(ts=soon.isoformat()))


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), "12.3", True])
def test_non_finite_or_non_numeric_values_rejected(bad: object) -> None:
    with pytest.raises(ValidationError):
        Reading.model_validate(_reading(value=bad))


def test_extra_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Reading.model_validate(_reading(colour="blue"))


def test_worst_quality_ordering() -> None:
    assert worst_quality(Quality.good, Quality.suspect) == Quality.suspect
    assert worst_quality(Quality.bad, Quality.suspect) == Quality.bad
    assert worst_quality(Quality.good, Quality.good) == Quality.good
