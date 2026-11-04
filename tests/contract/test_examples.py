"""Every file in contract/examples is accepted or rejected as its filename says."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from contract import IngestMessage

EXAMPLES = Path(__file__).resolve().parents[2] / "contract" / "examples"
VALID = sorted(EXAMPLES.glob("valid_*.json"))
INVALID = sorted(EXAMPLES.glob("invalid_*.json"))


def test_example_counts() -> None:
    assert len(VALID) == 3
    assert len(INVALID) == 8


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.stem)
def test_valid_examples_are_accepted(path: Path) -> None:
    msg = IngestMessage.model_validate(json.loads(path.read_text()))
    assert msg.readings
    for r in msg.readings:
        assert r.ts.tzinfo is not None and r.ts.utcoffset().total_seconds() == 0
        assert r.kind is not None and r.source is not None


@pytest.mark.parametrize("path", INVALID, ids=lambda p: p.stem)
def test_invalid_examples_are_rejected(path: Path) -> None:
    with pytest.raises(ValidationError):
        IngestMessage.model_validate(json.loads(path.read_text()))


def test_invalid_reasons_are_specific() -> None:
    expectations = {
        "invalid_naive_timestamp": "naive timestamp",
        "invalid_unknown_channel": "unknown channel",
        "invalid_wrong_unit": "must be 'degC'",
        "invalid_nan_value": "finite",
        "invalid_future_timestamp": "future",
        "invalid_empty_readings": "at least 1",
        "invalid_bad_quality_value": "quality",
        "invalid_duplicate_key_in_batch": "repeats the key",
    }
    for stem, needle in expectations.items():
        with pytest.raises(ValidationError) as exc:
            IngestMessage.model_validate(json.loads((EXAMPLES / f"{stem}.json").read_text()))
        assert needle in str(exc.value), stem
