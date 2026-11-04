"""Turn a ``World`` into contract-v1 messages, label posts, a fault log and the truth table."""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from contract.registry import load_registry
from sim import PO4_TO_P, SIM_FIRMWARE
from sim.faults import Fault, Measured, apply_sensor_faults
from sim.physics import World

log = logging.getLogger(__name__)

SENSOR_ROLES = ("intake", "downstream", "bench")
SAMPLED_ROLES = ("intake", "downstream", "effluent_input")
CRP_ROLES = ("intake", "downstream")


@dataclass
class Batch:
    """One HTTP call the simulator will make (or write in dry-run)."""

    endpoint: str  # "/ingest" or "/labels"
    sent_at: datetime
    node_id: str
    body: dict[str, Any]


@dataclass
class RunOutput:
    """Everything a run produces before talking to the API."""

    batches: list[Batch]
    faults: list[Fault]
    truth: pd.DataFrame
    readings_per_node_channel: Counter = field(default_factory=Counter)


def _iso(ts: datetime | pd.Timestamp) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


def _reading(ts: Any, channel: str, value: float, unit: str, **extra: Any) -> dict[str, Any]:
    r: dict[str, Any] = {
        "ts": _iso(ts),
        "channel": channel,
        "value": round(float(value), 4),
        "unit": unit,
    }
    r.update(extra)
    return r


def measure_nodes(world: World, rng: np.random.Generator) -> dict[str, Measured]:
    """Add sensor noise and TDS read spacing to the true series of every sensor node."""
    s = world.params["sensors"]
    n = world.grid.n_steps
    out: dict[str, Measured] = {}
    for node in world.nodes.values():
        if node.role not in SENSOR_ROLES:
            continue
        tds_mask = (np.arange(n) % int(s["tds_every_steps"])) == 0
        values = {
            "water_temp": node.water_temp + rng.normal(0, s["water_temp"], n),
            "ph": node.ph + rng.normal(0, s["ph"], n),
            "tds": node.tds + rng.normal(0, s["tds"], n),
            "turbidity": np.maximum(node.turbidity + rng.normal(0, s["turbidity"], n), 0.0),
        }
        present = {k: np.ones(n, dtype=bool) for k in values}
        present["tds"] = tds_mask
        out[node.node_id] = Measured(
            node_id=node.node_id,
            role=node.role,
            ts=world.ts,
            values=values,
            quality={k: np.full(n, "good", dtype=object) for k in values},
            reason={k: np.full(n, None, dtype=object) for k in values},
            present=present,
            tds_read_mask=tds_mask,
        )
    return out


def _raw_volts(channel: str, value: float, rng: np.random.Generator, noise: float) -> float:
    """Derive raw ADC volts from calibrated values (our linear assumptions)."""
    if channel == "ph":
        v = 0.30 * value
    elif channel == "tds":
        v = 2.3 * value / 1000.0
    else:  # turbidity: output voltage falls as turbidity rises
        v = 4.2 - 3.0 * min(value, 1000.0) / 1000.0
    return float(np.clip(v + rng.normal(0, noise), 0.0, 5.0))


def sensor_readings(
    m: Measured, world: World, rng: np.random.Generator
) -> list[tuple[pd.Timestamp, dict]]:
    """Contract readings of one node's sensors for every step (with raw volt channels)."""
    registry = load_registry()
    noise = world.params["sensors"]["raw_v_noise"]
    out: list[tuple[pd.Timestamp, dict]] = []
    for i, ts in enumerate(m.ts):
        for channel in ("water_temp", "ph", "tds", "turbidity"):
            if not m.present[channel][i]:
                continue
            value = float(m.values[channel][i])
            meta: dict[str, Any] = {"emulates": "sensor"}
            if m.reason[channel][i]:
                meta["reason"] = m.reason[channel][i]
            out.append(
                (
                    ts,
                    _reading(
                        ts,
                        channel,
                        value,
                        registry[channel].unit,
                        quality=str(m.quality[channel][i]),
                        meta=meta,
                    ),
                )
            )
            if channel != "water_temp":
                raw = _raw_volts(channel, value, rng, noise)
                out.append(
                    (ts, _reading(ts, f"{channel}_raw_v", raw, "V", meta={"emulates": "sensor"}))
                )
    return out


def external_readings(world: World) -> list[tuple[pd.Timestamp, dict]]:
    """Hourly discharge (emulates IBWC) and air_temp / precip (emulate weather)."""
    out: list[tuple[pd.Timestamp, dict]] = []
    for i, ts in enumerate(world.hourly_ts):
        out.append(
            (
                ts,
                _reading(
                    ts,
                    "discharge",
                    world.discharge_h[i],
                    "m3/s",
                    kind="external",
                    meta={"emulates": "IBWC", "station": "PLACEHOLDER"},
                ),
            )
        )
        out.append(
            (
                ts,
                _reading(
                    ts,
                    "air_temp",
                    world.air_temp_h[i],
                    "degC",
                    kind="external",
                    meta={"emulates": "weather"},
                ),
            )
        )
        out.append(
            (
                ts,
                _reading(
                    ts,
                    "precip",
                    world.precip_h_mm[i],
                    "mm",
                    kind="external",
                    meta={"emulates": "weather", "interval_min": 60},
                ),
            )
        )
    return out


def analyzer_readings(
    world: World, node_id: str, rng: np.random.Generator, faults_cfg: dict[str, Any]
) -> tuple[list[tuple[pd.Timestamp, dict]], list[Fault]]:
    """Colorimetric analyzer cycles every ``cycle_min`` with a reference run every ``ref_every``."""
    a = world.params["analyzer"]
    node = world.nodes[node_id]
    hours_step = np.arange(world.grid.n_steps) * world.grid.step_h
    cycle_h = a["cycle_min"] / 60.0
    n_cycles = int(world.grid.days * 24 / cycle_h)
    drift_cfg = faults_cfg.get("analyzer_ref_drift")
    faults: list[Fault] = []
    out: list[tuple[pd.Timestamp, dict]] = []
    a_offset = 0.0
    prev_ref_abs: float | None = None
    untrusted = False
    fault_start: datetime | None = None
    for k in range(n_cycles):
        h = k * cycle_h
        ts = pd.Timestamp(world.grid.start) + pd.Timedelta(hours=h)
        p_true = float(np.interp(h, hours_step, node.phosphate_po4))
        led = a["led_v"] * (1.0 - a["led_decay_per_day"] * h / 24.0)
        is_ref = k % int(a["ref_every"]) == 0
        extra: list[dict[str, Any]] = []
        if is_ref:
            step = rng.normal(0.0, a["ref_walk_sd_au"])
            in_fault = (
                drift_cfg is not None
                and drift_cfg["start_day"] * 24
                <= h
                < (drift_cfg["start_day"] + drift_cfg["duration_days"]) * 24
            )
            if in_fault:
                step += drift_cfg["drift_au_per_ref"]
                if fault_start is None:
                    fault_start = ts.to_pydatetime()
            elif fault_start is not None:
                faults.append(
                    Fault(
                        "analyzer_ref_drift",
                        node_id,
                        "phosphate_po4",
                        fault_start,
                        ts.to_pydatetime(),
                    )
                )
                fault_start = None
            a_offset += step
            ref_abs = a["a0_au"] + a_offset + rng.normal(0, a["noise_au"])
            ref_drift = 0.0 if prev_ref_abs is None else ref_abs - prev_ref_abs
            prev_ref_abs = ref_abs
            untrusted = abs(ref_drift) > a["ref_drift_threshold_au"]
            extra = [
                _reading(ts, "analyzer_ref_drift", ref_drift, "AU"),
                _reading(ts, "analyzer_led_level", led, "V"),
            ]
        absorbance = (
            a["a0_au"] + a_offset + a["eps_au_per_mg_l"] * p_true + rng.normal(0, a["noise_au"])
        )
        i0 = led + rng.normal(0, 0.002)
        i = i0 * 10 ** (-absorbance)
        reported = max((absorbance - a["a0_au"]) / a["eps_au_per_mg_l"], 0.0)
        readings = [
            _reading(ts, "phosphate_po4", reported, "mg/L"),
            _reading(ts, "analyzer_i0", i0, "V"),
            _reading(ts, "analyzer_i", i, "V"),
            _reading(ts, "analyzer_absorbance", absorbance, "AU"),
            *extra,
        ]
        for r in readings:
            r["meta"] = {"emulates": "analyzer", "cycle_id": k, "is_reference_run": is_ref}
            if untrusted:
                r["quality"] = "bad"
                r["meta"]["reason"] = "ref_drift"
            out.append((ts, r))
    if fault_start is not None:
        faults.append(
            Fault(
                "analyzer_ref_drift",
                node_id,
                "phosphate_po4",
                fault_start,
                pd.Timestamp(world.grid.end).to_pydatetime(),
            )
        )
    return out, faults


def label_posts(world: World, rng: np.random.Generator, dense: bool) -> list[Batch]:
    """Grab-sample and CRP-style labels posted to ``/labels`` (kind label, source simulator)."""
    lp = world.params["labels"]
    grid = world.grid
    hours_step = np.arange(grid.n_steps) * grid.step_h
    posts: list[Batch] = []
    sample_no = 0
    seen: set[tuple[str, str, str]] = set()

    def post(
        ts: pd.Timestamp,
        node_id: str,
        channel: str,
        value: float,
        unit: str,
        emulates: str,
        **meta: Any,
    ) -> None:
        nonlocal sample_no
        key = (node_id, channel, _iso(ts))
        if key in seen:  # two grab samples snapped to the same step: keep one (unique key)
            return
        seen.add(key)
        sample_no += 1
        body = {
            "ts": _iso(ts),
            "node_id": node_id,
            "channel": channel,
            "value": round(float(value), 4),
            "unit": unit,
            "source": "simulator",
            "sample_id": f"SIM-{sample_no:05d}",
            "meta": {"emulates": emulates, **meta},
        }
        posts.append(Batch("/labels", ts.to_pydatetime() + timedelta(hours=2), node_id, body))

    sampled = [n for n in world.params["_nodes"] if n["role"] in SAMPLED_ROLES]
    weeks = int(np.ceil(grid.days / 7))
    for node in sampled:
        for w in range(weeks):
            count = int(rng.integers(lp["grab_per_week"][0], lp["grab_per_week"][1] + 1))
            for _ in range(count):
                day = w * 7 + rng.integers(0, 7)
                if day >= grid.days:
                    continue
                hour = rng.uniform(*lp["grab_hours_utc"])
                h = day * 24 + hour
                ts = (pd.Timestamp(grid.start) + pd.Timedelta(hours=float(h))).round(
                    f"{grid.step_min}min"
                )
                h = (ts - pd.Timestamp(grid.start)).total_seconds() / 3600.0
                if node["role"] == "effluent_input":
                    e = world.params["effluent"]
                    tp = float(np.interp(h, hours_step, world.effluent_tp_as_p))
                    p_true = e["dissolved_frac"] * tp / PO4_TO_P
                else:
                    p_true = float(
                        np.interp(h, hours_step, world.nodes[node["node_id"]].phosphate_po4)
                    )
                err = rng.uniform(-1, 1) * (lp["hi713_abs_ppm"] + lp["hi713_rel"] * p_true)
                value = max(p_true + err, 0.0)
                meta: dict[str, Any] = {}
                if value > lp["hi713_max_ppm"]:
                    value = lp["hi713_max_ppm"]
                    meta["censored"] = True
                post(ts, node["node_id"], "phosphate_po4", value, "mg/L", "HI713", **meta)
    # CRP-style monthly grab samples at the river nodes
    months = pd.date_range(pd.Timestamp(grid.start).normalize(), pd.Timestamp(grid.end), freq="MS")
    for month in months:
        ts = month + pd.Timedelta(days=lp["crp_day_of_month"] - 1, hours=lp["crp_hour_utc"])
        if not (pd.Timestamp(grid.start) <= ts < pd.Timestamp(grid.end)):
            continue
        h = (ts - pd.Timestamp(grid.start)).total_seconds() / 3600.0
        for node in world.params["_nodes"]:
            if node["role"] not in CRP_ROLES:
                continue
            series = world.nodes[node["node_id"]]
            chl = float(np.interp(h, hours_step, series.chlorophyll_true))
            po4 = float(np.interp(h, hours_step, series.phosphate_po4))
            post(
                ts,
                node["node_id"],
                "chlorophyll_a",
                chl * (1 + rng.normal(0, lp["chl_noise_rel"])),
                "ug/L",
                "CRP",
            )
            tp = PO4_TO_P * po4 * lp["tp_over_po4_as_p"] * (1 + rng.normal(0, lp["tp_noise_rel"]))
            post(ts, node["node_id"], "total_phosphorus", max(tp, 0.0), "mg/L", "CRP")
    if dense:
        first = pd.Timestamp(grid.start).normalize()
        while first.weekday() != int(lp["dense_weekday"]):
            first += pd.Timedelta(days=1)
        for ts in pd.date_range(
            first + pd.Timedelta(hours=lp["crp_hour_utc"]), pd.Timestamp(grid.end), freq="7D"
        ):
            if ts >= pd.Timestamp(grid.end):
                continue
            h = (ts - pd.Timestamp(grid.start)).total_seconds() / 3600.0
            for node in world.params["_nodes"]:
                if node["role"] not in CRP_ROLES:
                    continue
                chl = float(np.interp(h, hours_step, world.nodes[node["node_id"]].chlorophyll_true))
                post(
                    ts,
                    node["node_id"],
                    "chlorophyll_a",
                    chl * (1 + rng.normal(0, lp["chl_noise_rel"])),
                    "ug/L",
                    "CRP",
                    cadence="weekly_dense",
                )
    return posts


def _message(node_id: str, sent_at: datetime, readings: list[dict]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "node_id": node_id,
        "source": "simulator",
        "sent_at": _iso(sent_at),
        "firmware": SIM_FIRMWARE,
        "readings": readings,
    }


def build_run(
    world: World, seed: int, *, dense_labels: bool, batch_hours: float = 24.0
) -> RunOutput:
    """Assemble all batches (chronological by ``sent_at``), faults and truth for a run."""
    rng_root = np.random.SeedSequence(seed + 1_000_003)
    rng_meas, rng_faults, rng_analyzer, rng_labels, rng_outage = (
        np.random.default_rng(s) for s in rng_root.spawn(5)
    )
    faults_cfg = world.params.get("faults") or {}
    measured = measure_nodes(world, rng_meas)
    faults = apply_sensor_faults(measured, faults_cfg, rng_faults)

    per_node: dict[str, list[tuple[pd.Timestamp, dict]]] = defaultdict(list)
    for node_id, m in measured.items():
        per_node[node_id].extend(sensor_readings(m, world, rng_meas))
        if m.role == "intake":
            per_node[node_id].extend(external_readings(world))
        if m.role == world.params["analyzer"]["node_role"]:
            readings, a_faults = analyzer_readings(world, node_id, rng_analyzer, faults_cfg)
            per_node[node_id].extend(readings)
            faults.extend(a_faults)

    # --- message-level faults -------------------------------------------------------------
    start = pd.Timestamp(world.grid.start)
    silent = faults_cfg.get("node_silent")
    if silent:
        for m in measured.values():
            if m.role != silent["node_role"]:
                continue
            s0 = start + pd.Timedelta(hours=silent["start_day"] * 24)
            s1 = s0 + pd.Timedelta(hours=silent["duration_h"])
            per_node[m.node_id] = [(ts, r) for ts, r in per_node[m.node_id] if not (s0 <= ts < s1)]
            faults.append(
                Fault("node_silent", m.node_id, "*", s0.to_pydatetime(), s1.to_pydatetime())
            )
    outages: dict[str, list[tuple[pd.Timestamp, pd.Timestamp]]] = defaultdict(list)
    outage = faults_cfg.get("wifi_outage")
    if outage:
        for m in measured.values():
            if m.role != outage["node_role"]:
                continue
            for _ in range(int(outage["episodes"])):
                day = int(rng_outage.integers(1, max(world.grid.days - 1, 2)))
                o0 = (
                    start + pd.Timedelta(hours=day * 24 + float(rng_outage.uniform(0, 18)))
                ).floor("min")
                o1 = o0 + pd.Timedelta(hours=outage["duration_h"])
                outages[m.node_id].append((o0, o1))
                faults.append(
                    Fault("wifi_outage", m.node_id, "*", o0.to_pydatetime(), o1.to_pydatetime())
                )

    batches: list[Batch] = []
    counts: Counter = Counter()
    for node_id, items in per_node.items():
        items.sort(key=lambda x: x[0])
        for _, r in items:
            counts[(node_id, r["channel"])] += 1
        deferred: list[tuple[pd.Timestamp, dict]] = []
        regular: list[tuple[pd.Timestamp, dict]] = []
        for ts, r in items:
            if any(o0 <= ts < o1 for o0, o1 in outages.get(node_id, [])):
                deferred.append((ts, r))
            else:
                regular.append((ts, r))
        # regular batches of about `batch_hours`
        groups: dict[int, list[dict]] = defaultdict(list)
        for ts, r in regular:
            groups[int((ts - start).total_seconds() // (batch_hours * 3600))].append(r)
        for g, readings in sorted(groups.items()):
            sent_at = (
                start + pd.Timedelta(hours=(g + 1) * batch_hours) + pd.Timedelta(seconds=5)
            ).to_pydatetime()
            batches.append(Batch("/ingest", sent_at, node_id, _message(node_id, sent_at, readings)))
        # late bulk upload after each outage: buffered readings plus some duplicates
        for o0, o1 in outages.get(node_id, []):
            buffered = [r for ts, r in deferred if o0 <= ts < o1]
            before = [r for ts, r in regular if o0 - pd.Timedelta(hours=24) <= ts < o0]
            n_dup = int(len(before) * outage["duplicate_frac"])
            dups = (
                [
                    before[i]
                    for i in sorted(rng_outage.choice(len(before), size=n_dup, replace=False))
                ]
                if before
                else []
            )
            sent_at = (o1 + pd.Timedelta(minutes=1)).to_pydatetime()
            batches.append(
                Batch("/ingest", sent_at, node_id, _message(node_id, sent_at, dups + buffered))
            )

    batches.extend(label_posts(world, rng_labels, dense_labels))
    for b in batches:
        if b.endpoint == "/labels":
            counts[(b.node_id, b.body["channel"])] += 1
    batches.sort(key=lambda b: (b.sent_at, b.node_id, b.endpoint))
    faults.sort(key=lambda f: (f.start, f.node_id, f.type))
    return RunOutput(
        batches=batches, faults=faults, truth=world.truth_frame(), readings_per_node_channel=counts
    )
