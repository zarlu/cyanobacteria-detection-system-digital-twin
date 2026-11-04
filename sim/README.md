# sim/ (owner: A): sensor-node simulator

Stands in for the ESP32 nodes, the phosphate analyzer, the HI713 grab samples, the CRP monthly
samples and the IBWC/weather connectors until the hardware and the connectors exist. It speaks
contract v1 over HTTP exactly like a node would (`POST /ingest` for readings, `POST /labels` for
labels) and nothing else; in `--dry-run` it writes the same requests as JSONL instead.

Everything it sends has `source='simulator'` and `meta.emulates` set to the real source it imitates
(`sensor`, `analyzer`, `HI713`, `CRP`, `IBWC`, `weather`). The dashboard shows a banner whenever such
rows are displayed. Results on simulated data are **pipeline validation only, not river performance**.

## Usage

```sh
make sim ARGS="--scenario summer_bloom --days 90 --seed 1 --dense-labels"   # backfill into the running API
make sim ARGS="--scenario sensor_faults --days 14 --dry-run --start-date 2026-09-01"
make sim ARGS="--scenario baseline --days 1 --mode live --speedup 60"
```

| option | default | meaning |
|---|---|---|
| `--scenario` | `baseline` | `baseline`, `summer_bloom`, `effluent_step`, `storm_week`, `sensor_faults` (in `scenarios.yaml`) |
| `--days N` | 90 | horizon |
| `--step-min M` | 15 | node sampling step |
| `--seed S` | 1 | same seed, same options -> byte-identical output |
| `--start-date` | N days before now (UTC) | `YYYY-MM-DD`, UTC midnight |
| `--mode` | `backfill` | `backfill` posts history as fast as the API accepts it, one message per node per day; `live` paces the same batches (one per node per step) in wall-clock time divided by `--speedup` |
| `--api-url`, `--api-key` | `API_URL`, `API_KEY` env | where to post |
| `--dry-run` | off | write `sim_out/<scenario>_seed<S>.jsonl` instead of posting |
| `--dense-labels` | off | add a weekly `chlorophyll_a` label series (for the bloom-risk task) |
| `--out-dir` | `sim_out/` | where `truth.csv`, `faults.csv` and dry-run JSONL go (gitignored) |

Outputs: `sim_out/truth.csv` (the hidden true series per node and step, including
`chlorophyll_a_true`; never sent to the API), `sim_out/faults.csv` (every injected fault:
`type,node_id,channel,start,end`), and an end-of-run summary with readings per node and channel
and a fault table.

Nodes come from `config/reach.yaml` by role: `intake` and `downstream` are river sensor nodes
(`intake` also carries the external series, `downstream` hosts the analyzer), `effluent_input`
only gets grab-sample labels (it is a source term, not a sensor site), `bench` is a still bucket.

## Assumptions of ours, not Rio Grande facts

All parameters live in `scenarios.yaml` (`defaults`, overridden per scenario). Nothing below is
calibrated to the Rio Grande; the shapes are chosen so the pipeline sees seasonality, storms,
dilution, an effluent step and a bloom. Time `t` in hours from the start, `hod` hour of day (UTC),
`doy` day of year. Noise terms are AR(1) or white Gaussian with the standard deviations in the YAML.

- **Air temperature** `air = mean + A_s cos(2π(doy - peak)/365.25) + A_d cos(2π(hod - h_peak)/24) + AR(1)`, `h_peak = diurnal_peak_hour_utc` (21 UTC, mid-afternoon in America/Chicago).
- **Storms**: starts are Poisson with `rate_per_day` (a higher `window_rate_per_day` inside
  `window_days` for `storm_week`); each has uniform duration and intensity. `precip` per hour is
  the sum of active intensities.
- **Discharge** `Q = Q_base (1 + AR(1)) + Σ pulses`, floor `floor_frac · Q_base` so `Q > 0`. Each
  storm adds a unit hydrograph: linear rise to `runoff_m3s_per_mm · total_mm` over `rise_h`, then
  `exp(-(τ - rise)/recession_tau_h)`.
- **Water temperature (upstream)** seasonal + diurnal (peak `diurnal_peak_hour_utc`, 22 UTC) + AR(1); downstream =
  upstream + `downstream_delta`.
- **Turbidity (upstream)** `base + per_m3s · max(Q(t - lag_h) - Q_base, 0) + slow sinus + noise`,
  floor 0.5 NTU; downstream adds `downstream_add_ntu`.
- **TDS (upstream)** `base · (Q_base / Q)^dilution_exp + noise`; downstream is the flow-weighted mix
  with the effluent: `(Q·tds_up + Q_eff·tds_eff) / (Q + Q_eff)`.
- **Effluent** `Q_eff` with a ±`diurnal_frac` daily swing; total phosphorus `tp_mg_l_as_p` (as P),
  stepping to `step_tp_mg_l_as_p` at `step_day` (2.82 mg/L as P in `effluent_step` is the only
  number taken from our documents: the 2020 permit application). Dissolved PO4 =
  `dissolved_frac · tp / 0.3261`.
- **Phosphate as PO4 (upstream)** `background + storm_release · max(Q - Q_base, 0)/Q_base + noise`;
  downstream is the flow-weighted mix with the effluent PO4.
- **Hidden chlorophyll-a** logistic growth per step:
  `dC/dt = C · (r_max · e^{k(T - T_ref)} · P/(P + K_s) - mortality - flush) · (1 - C/K)`,
  `flush = flush_per_day · max(Q/flush_q - 1, 0)`, floor `min_ug_l`. In `summer_bloom` (hot, low
  flow, high phosphate) it climbs to the carrying capacity; in `baseline` it never grows.
- **pH** `base + A_ph (1 + c·C) cos(2π(hod - h_peak)/24) + o·C/10 + AR(1)` (same peak hour as water temperature), clipped to [6, 9.5]:
  photosynthesis raises daytime pH more when chlorophyll is high.
- **Sensors** add Gaussian noise; TDS is read every `tds_every_steps` steps (the firmware spaces
  TDS reads because the probe disturbs pH). Raw volts are linear maps of the calibrated value
  (`ph_raw_v = 0.30·pH`, `tds_raw_v = 2.3·tds/1000`, `turbidity_raw_v = 4.2 - 3·turbidity/1000`)
  plus small noise, clipped to 0..5 V.
- **Analyzer** (downstream node) every `cycle_min`: absorbance `A = a0 + offset + ε·P + noise`,
  `I0 = led(t)`, `I = I0 · 10^{-A}`, reported `phosphate_po4 = (A - a0)/ε` (so a drifted reference
  biases it, like a real instrument). Every `ref_every`-th cycle is a reference run that reports
  `analyzer_ref_drift` (change of the blank since the previous reference, a random walk) and
  `analyzer_led_level`. When `|drift| > ref_drift_threshold_au` the cycles until the next reference
  run are sent as `quality='bad'`, `meta.reason='ref_drift'`.
- **Labels** (`kind='label'`, posted to `/labels`): HI713 grab samples of `phosphate_po4` 2 to 3
  per week per sampled node at a daytime hour, error uniform within ±(0.04 + 0.04·value) ppm
  (the HI713 spec bound), censored at 2.5 ppm with `meta.censored=true`; CRP-style
  `chlorophyll_a` and `total_phosphorus` on the 15th of each month at the river nodes, with
  `total_phosphorus (as P) = 0.3261 · PO4 · tp_over_po4_as_p`; with `--dense-labels` a weekly
  `chlorophyll_a` series on Mondays (`meta.cadence='weekly_dense'`).
- **External series** (`kind='external'`, on the intake node): hourly `discharge` (emulates IBWC,
  `meta.original_unit='ft3/s'`, `meta.station='PLACEHOLDER'`), `air_temp` and `precip`
  (emulate weather, `meta.interval_min=60`).

## Faults (`faults.py`, logged to `sim_out/faults.csv`)

| type | what happens | who flags it |
|---|---|---|
| `turbidity_drift` | reading = true + `ntu_per_8h (1 - e^{-h/8})(1 + h/168)` since the last weekly cleaning | nobody (QC has no drift rule) |
| `ph_settling` | after every TDS read pH dips by `dip` and recovers within `settle_min`; firmware sends `suspect` + `ph_settling` | the node |
| `stuck` | a channel repeats one value for `duration_h` | server QC (`stuck`, from the 12th identical value) |
| `spike` | one reading moved by `step_multiple × max_step_15min` of the channel, either sign | server QC (`spike`, or `out_of_range` when the spike leaves the valid range) |
| `wifi_outage` | the node buffers `duration_h` of readings and uploads them late with old timestamps, plus `duplicate_frac` of the previous day's readings re-sent | nothing to flag; duplicates must not create rows |
| `analyzer_ref_drift` | the reference random walk gets `drift_au_per_ref` added for `duration_days` | the analyzer (`bad`, `ref_drift`) |
| `node_silent` | the node sends nothing for `duration_h` | `/nodes.minutes_since_seen` (and later the `sensor_silent` alert) |

Fault `episodes` are per targeted node (a fault without `node_role` applies to every sensor node).

## Measured QC detection (not assumed)

`uv run python scripts/qc_detection_rates.py --days 30` ingests `sensor_faults` through the real
API and prints the rates; `tests/sim/test_backfill.py` asserts floors below them. The numbers
and the limitations they reveal are in `docs/ASSUMPTIONS.md` (Phase 2) and `docs/HANDOFF.md`.
