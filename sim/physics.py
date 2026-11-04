"""Physically motivated (not river-realistic) series for the simulator.

Every equation and parameter here is an assumption of ours, documented in ``sim/README.md``
under "Assumptions of ours, not Rio Grande facts". Nothing is calibrated to the Rio Grande.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from sim import PO4_TO_P


@dataclass
class TimeGrid:
    """Regular UTC step grid for the run."""

    start: datetime
    days: int
    step_min: int

    def __post_init__(self) -> None:
        if self.start.tzinfo is None:
            raise ValueError("TimeGrid.start must be an aware UTC datetime")
        self.start = self.start.astimezone(UTC).replace(second=0, microsecond=0)

    @property
    def n_steps(self) -> int:
        return int(self.days * 24 * 60 / self.step_min)

    @property
    def step_h(self) -> float:
        return self.step_min / 60.0

    @property
    def end(self) -> datetime:
        return self.start + timedelta(days=self.days)

    def ts(self) -> pd.DatetimeIndex:
        return pd.date_range(self.start, periods=self.n_steps, freq=f"{self.step_min}min")

    def hourly(self) -> pd.DatetimeIndex:
        return pd.date_range(self.start, periods=self.days * 24, freq="1h")


@dataclass
class NodeSeries:
    """True (hidden) series for one node on the step grid."""

    node_id: str
    role: str
    water_temp: np.ndarray
    ph: np.ndarray
    tds: np.ndarray
    turbidity: np.ndarray
    phosphate_po4: np.ndarray
    chlorophyll_true: np.ndarray


@dataclass
class World:
    """All true series: externals on the hourly grid, node series on the step grid."""

    grid: TimeGrid
    params: dict[str, Any]
    ts: pd.DatetimeIndex
    hourly_ts: pd.DatetimeIndex
    air_temp_h: np.ndarray
    precip_h_mm: np.ndarray
    discharge_h: np.ndarray
    discharge: np.ndarray  # on the step grid
    effluent_q: np.ndarray  # m3/s on the step grid
    effluent_tp_as_p: np.ndarray  # mg/L as P on the step grid
    nodes: dict[str, NodeSeries] = field(default_factory=dict)

    def truth_frame(self) -> pd.DataFrame:
        """Long table of hidden truth per node and step (written to ``sim_out/truth.csv``)."""
        frames = []
        for n in self.nodes.values():
            frames.append(
                pd.DataFrame(
                    {
                        "ts": self.ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "node_id": n.node_id,
                        "water_temp": n.water_temp,
                        "ph": n.ph,
                        "tds": n.tds,
                        "turbidity": n.turbidity,
                        "phosphate_po4": n.phosphate_po4,
                        "chlorophyll_a_true": n.chlorophyll_true,
                        "discharge": self.discharge,
                        "effluent_q": self.effluent_q,
                        "effluent_tp_as_p": self.effluent_tp_as_p,
                    }
                )
            )
        return pd.concat(frames, ignore_index=True)


def _ar1(rng: np.random.Generator, n: int, sd: float, ar: float) -> np.ndarray:
    eps = rng.normal(0.0, sd * np.sqrt(max(1.0 - ar * ar, 1e-6)), n)
    out = np.empty(n)
    acc = 0.0
    for i in range(n):
        acc = ar * acc + eps[i]
        out[i] = acc
    return out


def _seasonal(ts: pd.DatetimeIndex, mean: float, amp: float, peak_doy: float) -> np.ndarray:
    doy = ts.day_of_year.to_numpy()
    return mean + amp * np.cos(2 * np.pi * (doy - peak_doy) / 365.25)


def _diurnal(ts: pd.DatetimeIndex, amp: float, peak_hour: float) -> np.ndarray:
    hod = ts.hour.to_numpy() + ts.minute.to_numpy() / 60.0
    return amp * np.cos(2 * np.pi * (hod - peak_hour) / 24.0)


def _storms(
    p: dict[str, Any], grid: TimeGrid, rng: np.random.Generator
) -> list[tuple[float, float, float]]:
    """Storm events as (start_h, duration_h, intensity_mm_h)."""
    events = []
    window = p.get("window_days")
    for day in range(grid.days):
        rate = p["rate_per_day"]
        if window and window[0] <= day < window[1] and p.get("window_rate_per_day") is not None:
            rate = p["window_rate_per_day"]
        for _ in range(rng.poisson(rate)):
            start_h = day * 24 + rng.uniform(0, 24)
            dur = rng.uniform(*p["duration_h"])
            inten = rng.uniform(*p["intensity_mm_h"])
            events.append((start_h, dur, inten))
    return sorted(events)


def _hydrograph(
    hours: np.ndarray, start_h: float, total_mm: float, p: dict[str, Any]
) -> np.ndarray:
    tau = hours - start_h
    rise, rec = p["rise_h"], p["recession_tau_h"]
    peak = p["runoff_m3s_per_mm"] * total_mm
    out = np.zeros_like(hours)
    rising = (tau >= 0) & (tau < rise)
    out[rising] = peak * tau[rising] / rise
    falling = tau >= rise
    out[falling] = peak * np.exp(-(tau[falling] - rise) / rec)
    return out


def generate_world(
    params: dict[str, Any], grid: TimeGrid, seed: int, nodes: list[dict[str, Any]]
) -> World:
    """Generate every hidden series for the run. Deterministic for (params, grid, seed, nodes)."""
    root = np.random.SeedSequence(seed)
    streams = root.spawn(8)
    rng_air, rng_storm, rng_q, rng_up, rng_down, rng_bench, rng_ph, rng_p = (
        np.random.default_rng(s) for s in streams
    )
    ts = grid.ts()
    hourly = grid.hourly()
    hours_step = np.arange(grid.n_steps) * grid.step_h
    hours_h = np.arange(len(hourly)).astype(float)

    # --- weather --------------------------------------------------------------------------
    a = params["air_temp"]
    air_h = (
        _seasonal(hourly, a["mean"], a["season_amp"], a["peak_doy"])
        + _diurnal(hourly, a["diurnal_amp"], a["diurnal_peak_hour_utc"])
        + _ar1(rng_air, len(hourly), a["noise_sd"], a["ar"])
    )
    storms = _storms(params["storms"], grid, rng_storm)
    precip_h = np.zeros(len(hourly))
    q_add_h = np.zeros(len(hourly))
    for start_h, dur, inten in storms:
        active = (hours_h >= start_h) & (hours_h < start_h + dur)
        precip_h[active] += inten  # mm per hourly interval
        q_add_h += _hydrograph(hours_h, start_h + dur, inten * dur, params["storms"])

    # --- discharge --------------------------------------------------------------------------
    d = params["discharge"]
    q_h = d["base_m3s"] * (1.0 + _ar1(rng_q, len(hourly), d["noise_frac"], d["ar"])) + q_add_h
    q_h = np.maximum(q_h, d["floor_frac"] * d["base_m3s"])
    q_step = np.interp(hours_step, hours_h, q_h)
    air_step = np.interp(hours_step, hours_h, air_h)

    # --- effluent (source term) ------------------------------------------------------------
    e = params["effluent"]
    eff_q = e["q_m3s"] * (1.0 + e["diurnal_frac"] * np.cos(2 * np.pi * (hours_step % 24 - 20) / 24))
    eff_tp = np.full(grid.n_steps, float(e["tp_mg_l_as_p"]))
    if e.get("step_day") is not None:
        eff_tp[hours_step >= e["step_day"] * 24] = float(e["step_tp_mg_l_as_p"])
    eff_po4 = e["dissolved_frac"] * eff_tp / PO4_TO_P

    # --- upstream (intake) -----------------------------------------------------------------
    w, t, tdsp, php, pp, chl = (
        params["water_temp"],
        params["turbidity"],
        params["tds"],
        params["ph"],
        params["phosphate"],
        params["chlorophyll"],
    )
    temp_up = (
        _seasonal(ts, w["mean"], w["season_amp"], w["peak_doy"])
        + _diurnal(ts, w["diurnal_amp"], w["diurnal_peak_hour_utc"])
        + _ar1(rng_up, grid.n_steps, w["noise_sd"], w["ar"])
    )
    temp_down = temp_up + w["downstream_delta"]
    q_lag = np.interp(hours_step - t["lag_h"], hours_step, q_step)
    excess = np.maximum(q_lag - d["base_m3s"], 0.0)
    slow = t["slow_amp_ntu"] * np.sin(2 * np.pi * hours_step / (t["slow_period_d"] * 24))
    turb_up = np.maximum(
        t["base_ntu"]
        + t["per_m3s"] * excess
        + slow
        + rng_up.normal(0, t["noise_ntu"], grid.n_steps),
        0.5,
    )
    turb_down = turb_up + t["downstream_add_ntu"]
    tds_up = tdsp["base_ppm"] * (d["base_m3s"] / q_step) ** tdsp["dilution_exp"]
    tds_up = tds_up + rng_up.normal(0, tdsp["noise_ppm"], grid.n_steps)
    tds_down = (q_step * tds_up + eff_q * tdsp["effluent_ppm"]) / (q_step + eff_q)
    rel_excess = np.maximum(q_step - d["base_m3s"], 0.0) / d["base_m3s"]
    po4_up = np.maximum(
        pp["background_po4"]
        + pp["storm_release_po4"] * rel_excess
        + rng_p.normal(0, pp["noise_po4"], grid.n_steps),
        0.0,
    )
    po4_down = (q_step * po4_up + eff_q * eff_po4) / (q_step + eff_q)
    po4_down = np.maximum(po4_down + rng_p.normal(0, pp["noise_po4"], grid.n_steps), 0.0)

    def chlorophyll(temp: np.ndarray, po4: np.ndarray) -> np.ndarray:
        c = np.empty(grid.n_steps)
        value = chl["initial_ug_l"]
        dt_days = grid.step_h / 24.0
        for i in range(grid.n_steps):
            growth = (
                chl["r_max_per_day"]
                * np.exp(chl["temp_k"] * (temp[i] - chl["temp_ref"]))
                * po4[i]
                / (po4[i] + chl["half_sat_po4"])
            )
            flush = chl["flush_per_day"] * max(q_step[i] / chl["flush_q_m3s"] - 1.0, 0.0)
            net = (growth - chl["mortality_per_day"] - flush) * (1.0 - value / chl["carrying_ug_l"])
            value = max(value * (1.0 + net * dt_days), chl["min_ug_l"])
            c[i] = value
        return c

    chl_up = chlorophyll(temp_up, po4_up)
    chl_down = chlorophyll(temp_down, po4_down)

    def ph_series(c: np.ndarray, rng: np.random.Generator) -> np.ndarray:
        diurnal = _diurnal(ts, php["diurnal_amp"], w["diurnal_peak_hour_utc"]) * (
            1.0 + php["chl_coupling"] * c
        )
        return np.clip(
            php["base"]
            + diurnal
            + php["chl_offset"] * c / 10.0
            + _ar1(rng, grid.n_steps, php["noise_sd"], 0.8),
            6.0,
            9.5,
        )

    ph_up = ph_series(chl_up, rng_ph)
    ph_down = ph_series(chl_down, rng_down)

    b = params["bench"]
    bench = NodeSeries(
        node_id="",
        role="bench",
        water_temp=b["water_temp"]
        + b["air_coupling"] * (air_step - a["mean"])
        + rng_bench.normal(0, 0.03, grid.n_steps),
        ph=b["ph"] + rng_bench.normal(0, 0.01, grid.n_steps),
        tds=b["tds"] + rng_bench.normal(0, 1.0, grid.n_steps),
        turbidity=np.maximum(b["turbidity"] + rng_bench.normal(0, 0.3, grid.n_steps), 0.1),
        phosphate_po4=np.full(grid.n_steps, 0.05),
        chlorophyll_true=np.full(grid.n_steps, chl["min_ug_l"]),
    )

    world = World(
        grid=grid,
        params=params,
        ts=ts,
        hourly_ts=hourly,
        air_temp_h=air_h,
        precip_h_mm=precip_h,
        discharge_h=q_h,
        discharge=q_step,
        effluent_q=eff_q,
        effluent_tp_as_p=eff_tp,
    )
    by_role = {
        "intake": NodeSeries("", "intake", temp_up, ph_up, tds_up, turb_up, po4_up, chl_up),
        "downstream": NodeSeries(
            "", "downstream", temp_down, ph_down, tds_down, turb_down, po4_down, chl_down
        ),
        "bench": bench,
    }
    for node in nodes:
        role = node["role"]
        if role in by_role:
            series = by_role[role]
            series.node_id = node["node_id"]
            world.nodes[node["node_id"]] = series
    return world
