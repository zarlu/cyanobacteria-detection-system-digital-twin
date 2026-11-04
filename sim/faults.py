"""Fault injection for the simulator. Every fault is logged to ``sim_out/faults.csv``.

Faults act on the *measured* arrays of a node (never on the hidden truth). Faults the firmware
would know about (``ph_settling``, analyzer ``ref_drift``) set quality and reason the way a real
node would; the others (drift, stuck, spikes) stay ``good`` so the server QC has to catch them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from contract.registry import load_registry


@dataclass(frozen=True)
class Fault:
    """One injected fault episode."""

    type: str
    node_id: str
    channel: str
    start: datetime
    end: datetime

    def as_row(self) -> dict[str, str]:
        return {
            "type": self.type,
            "node_id": self.node_id,
            "channel": self.channel,
            "start": self.start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": self.end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }


@dataclass
class Measured:
    """Measured arrays of one node on the step grid (values, quality, reason, presence)."""

    node_id: str
    role: str
    ts: pd.DatetimeIndex
    values: dict[str, np.ndarray]
    quality: dict[str, np.ndarray]
    reason: dict[str, np.ndarray]
    present: dict[str, np.ndarray]
    tds_read_mask: np.ndarray  # steps at which the TDS probe was read


def _nodes_with_role(measured: dict[str, Measured], role: str) -> list[Measured]:
    return [m for m in measured.values() if m.role == role]


def _next_weekly(ts0: pd.Timestamp, weekday: int, hour: int) -> pd.Timestamp:
    cand = ts0.normalize() + pd.Timedelta(hours=hour)
    while cand.weekday() != weekday or cand < ts0:
        cand += pd.Timedelta(days=1)
    return cand


def apply_turbidity_drift(
    m: Measured, cfg: dict[str, Any], _rng: np.random.Generator
) -> list[Fault]:
    """Turbidity reads high by a drift that builds over about 8 h and is reset by weekly cleaning.

    drift(h) = ntu_per_8h * (1 - exp(-h / 8)) * (1 + h / 168), h = hours since the last cleaning.
    """
    ts = m.ts
    cleanings = [ts[0]]
    nxt = _next_weekly(ts[0], cfg["cleaning_weekday"], cfg["cleaning_hour_utc"])
    while nxt < ts[-1]:
        cleanings.append(nxt)
        nxt += pd.Timedelta(days=7)
    cleanings.append(ts[-1] + pd.Timedelta(minutes=1))
    faults = []
    drift = np.zeros(len(ts))
    for a, b in zip(cleanings[:-1], cleanings[1:], strict=True):
        mask = (ts >= a) & (ts < b)
        hours = (ts[mask] - a).total_seconds().to_numpy() / 3600.0
        drift[mask] = cfg["ntu_per_8h"] * (1 - np.exp(-hours / 8.0)) * (1 + hours / 168.0)
        faults.append(
            Fault("turbidity_drift", m.node_id, "turbidity", a.to_pydatetime(), b.to_pydatetime())
        )
    m.values["turbidity"] = m.values["turbidity"] + drift
    return faults


def apply_ph_settling(m: Measured, cfg: dict[str, Any], _rng: np.random.Generator) -> list[Fault]:
    """The TDS probe disturbs pH for about 10 minutes; firmware flags those reads ``suspect``."""
    dip = cfg.get("dip", -0.3)
    settle = int(cfg.get("settle_min", 10))
    faults = []
    step_min = int((m.ts[1] - m.ts[0]).total_seconds() // 60)
    for i in np.flatnonzero(m.tds_read_mask):
        # every pH read within `settle` minutes after the TDS read is disturbed
        for j in range(i, len(m.ts)):
            minutes = (j - i) * step_min
            if minutes >= settle:
                break
            m.values["ph"][j] += dip * np.exp(-minutes / 4.0)
            m.quality["ph"][j] = "suspect"
            m.reason["ph"][j] = "ph_settling"
        start = m.ts[i].to_pydatetime()
        faults.append(
            Fault("ph_settling", m.node_id, "ph", start, start + timedelta(minutes=settle))
        )
    return faults


def apply_stuck(m: Measured, cfg: dict[str, Any], rng: np.random.Generator) -> list[Fault]:
    """A sensor repeats one value for ``duration_h`` hours; quality stays good, QC must catch it."""
    n = len(m.ts)
    step_h = (m.ts[1] - m.ts[0]).total_seconds() / 3600.0
    length = max(int(cfg["duration_h"] / step_h), 2)
    faults = []
    for _ in range(int(cfg["episodes"])):
        channel = str(rng.choice(cfg["channels"]))
        start = int(rng.integers(0, n - length))
        m.values[channel][start : start + length] = m.values[channel][start]
        faults.append(
            Fault(
                "stuck",
                m.node_id,
                channel,
                m.ts[start].to_pydatetime(),
                m.ts[start + length - 1].to_pydatetime(),
            )
        )
    return faults


def apply_spikes(m: Measured, cfg: dict[str, Any], rng: np.random.Generator) -> list[Fault]:
    """Isolated one-step spikes of ``step_multiple`` x the channel's ``max_step_15min``."""
    registry = load_registry()
    days = (m.ts[-1] - m.ts[0]).total_seconds() / 86400.0
    count = rng.poisson(cfg["rate_per_day"] * days)
    faults = []
    for _ in range(count):
        channel = str(rng.choice(cfg["channels"]))
        step = registry[channel].max_step_15min
        if step is None:
            continue
        candidates = np.flatnonzero(m.present[channel])[1:-1]  # never the first or last reading
        i = int(rng.choice(candidates))
        magnitude = rng.uniform(*cfg["step_multiple"]) * step * rng.choice([-1.0, 1.0])
        m.values[channel][i] += magnitude
        at = m.ts[i].to_pydatetime()
        faults.append(Fault("spike", m.node_id, channel, at, at))
    return faults


FAULT_APPLIERS = {
    "turbidity_drift": apply_turbidity_drift,
    "ph_settling": apply_ph_settling,
    "stuck": apply_stuck,
    "spikes": apply_spikes,
}


def apply_sensor_faults(
    measured: dict[str, Measured], faults_cfg: dict[str, Any], rng: np.random.Generator
) -> list[Fault]:
    """Apply every configured sensor fault. Returns the fault log entries."""
    log: list[Fault] = []
    for name, cfg in faults_cfg.items():
        if name not in FAULT_APPLIERS:
            continue  # message-level and analyzer faults are handled by the builder
        cfg = cfg or {}
        if "node_role" in cfg:
            targets = _nodes_with_role(measured, cfg["node_role"])
        else:
            targets = [m for m in measured.values() if m.role in ("intake", "downstream", "bench")]
        for m in targets:
            log.extend(FAULT_APPLIERS[name](m, cfg, rng))
    return log
