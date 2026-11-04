"""Enumerations of the data contract (CLAUDE.md section 5)."""

from enum import StrEnum


class Kind(StrEnum):
    """What a reading is: a sensor measurement, a lab label, or an external series."""

    measurement = "measurement"
    label = "label"
    external = "external"


class Source(StrEnum):
    """Who produced a reading."""

    sensor = "sensor"
    analyzer = "analyzer"
    HI713 = "HI713"
    CRP = "CRP"
    IBWC = "IBWC"
    weather = "weather"
    manual = "manual"
    simulator = "simulator"


class Quality(StrEnum):
    """Quality flag. Ordered good < suspect < bad; QC only ever moves rightwards."""

    good = "good"
    suspect = "suspect"
    bad = "bad"


QUALITY_RANK: dict[Quality, int] = {Quality.good: 0, Quality.suspect: 1, Quality.bad: 2}


def worst_quality(a: Quality, b: Quality) -> Quality:
    """Return the worse of two quality flags."""
    return a if QUALITY_RANK[a] >= QUALITY_RANK[b] else b
