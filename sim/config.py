"""Scenario loading: ``sim/scenarios.yaml`` defaults deep-merged with one scenario's overrides."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

SCENARIOS_YAML = Path(__file__).with_name("scenarios.yaml")


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = deepcopy(value)
    return out


def scenario_names(path: Path | None = None) -> list[str]:
    """Names of the scenarios defined in the YAML."""
    raw = yaml.safe_load((path or SCENARIOS_YAML).read_text(encoding="utf-8"))
    return list(raw["scenarios"])


def load_scenario(name: str, path: Path | None = None) -> dict[str, Any]:
    """Return the fully resolved parameter dict for scenario ``name``."""
    raw = yaml.safe_load((path or SCENARIOS_YAML).read_text(encoding="utf-8"))
    if name not in raw["scenarios"]:
        raise KeyError(f"unknown scenario {name!r}; choose one of {list(raw['scenarios'])}")
    merged = _deep_merge(raw["defaults"], raw["scenarios"][name] or {})
    merged["name"] = name
    return merged
