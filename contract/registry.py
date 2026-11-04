"""Channel registry loaded from ``contract/channels.yaml`` (the source of truth)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from contract.enums import Kind

CHANNELS_YAML = Path(__file__).with_name("channels.yaml")


class ChannelSpec(BaseModel):
    """One row of the channel registry."""

    model_config = ConfigDict(frozen=True)

    channel: str
    unit: str
    default_kind: Kind
    valid_min: float
    valid_max: float
    max_step_15min: float | None = None
    stuck_after: int | None = None
    saturation_min: float | None = None  # values at or above are flagged `saturated`
    description: str = ""


@lru_cache(maxsize=1)
def load_registry(path: Path | None = None) -> dict[str, ChannelSpec]:
    """Load and validate the channel registry; cached after the first call."""
    raw = yaml.safe_load((path or CHANNELS_YAML).read_text(encoding="utf-8"))
    channels = raw["channels"]
    return {name: ChannelSpec(channel=name, **fields) for name, fields in channels.items()}


def get_channel(name: str) -> ChannelSpec | None:
    """Return the spec for ``name`` or None when the channel is unknown."""
    return load_registry().get(name)
