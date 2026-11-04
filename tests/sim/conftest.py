"""Shared helpers for simulator tests."""

from __future__ import annotations

import pytest

from sim.build import RunOutput
from sim.run import generate, parse_args

START = "2026-08-01"


def run_output(scenario: str, days: int = 30, seed: int = 1, *extra: str) -> RunOutput:
    """Generate a run (no I/O) with a fixed start date."""
    argv = ["--scenario", scenario, "--days", str(days), "--seed", str(seed), "--start-date", START]
    return generate(parse_args([*argv, *extra]))


@pytest.fixture(scope="session")
def sensor_faults_30d() -> RunOutput:
    return run_output("sensor_faults", 30, 1)
