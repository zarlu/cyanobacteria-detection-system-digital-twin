# Foundation handoff (Phases 0 to 3)

For developers **A** (backend, alerts, firmware), **B** (ML) and **C** (dashboard). Built by one
agent on branch `feat/foundation-p0-p3`, tags `p0-done` to `p3-done` and `foundation-done`.
Read `CLAUDE.md` first; it is the rulebook. Every number below comes from a command that was run
on 2026-11-03 on an Apple Silicon laptop (Python 3.13.7); see `docs/ASSUMPTIONS.md` for the full log.

## 1. Repo map

| path | state | what it is |
|---|---|---|
| `CLAUDE.md` | rulebook | contract, DB, API, ML and working agreements. Section 3 now says Python >= 3.12 (see deviations) |
| `pyproject.toml`, `uv.lock`, `Makefile` | built | exact pins, `uv` environment, every `make` target of CLAUDE.md section 3 |
| `contract/` | built | `enums.py`, `registry.py` (loads `channels.yaml`), `models.py` (`Reading`, `IngestEnvelope`, strict `IngestMessage`), `CONTRACT.md`, `examples/` (3 valid, 8 invalid) |
| `db/` | built | SQLAlchemy 2.0 models for all six tables (`models`, `predictions`, `alerts` are created but unused so far), WAL engine, seeding, `python -m db.reset` |
| `api/` | built | `app.py` factory, `deps.py`, `settings.py`, `qc.py`, `ingest.py`, routers `health`, `ingest`, `nodes`, `channels`, `reach`, `observations`, `labels` |
| `sim/` | built | `scenarios.yaml`, `config.py`, `physics.py`, `faults.py`, `build.py`, `client.py`, `run.py`; README with every equation |
| `dashboard/` | v0 | `api_client.py`, `helpers.py`, `ui.py`, `views/overview.py`, `views/live_signals.py`, `app.py` |
| `config/reach.yaml` | placeholders | 4 nodes in river order; every coordinate and distance is `PLACEHOLDER` |
| `scripts/` | tools | `bench_p1.py` (API timings), `qc_detection_rates.py` (QC vs injected faults) |
| `tests/` | 126 tests | `contract/`, `db/`, `api/`, `sim/`, `dashboard/` (120 unit, `make test` about 30 s) and `e2e/` (6, `make test-e2e` about 8 s) |
| `docs/` | | `ASSUMPTIONS.md` (every decision), this file |
| `ml/`, `alerts/`, `connectors/`, `firmware/` | **stubs** | README only; nothing else may live there until their phase |
| `.github/workflows/ci.yml` | built | `make lint` + `make test` on push and PR (not yet run on GitHub: no remote) |

Gitignored and never committed: `.env`, `data/` (`twin.db`, `demo.db`, `raw/crp/`), `artifacts/`,
`.run/` (demo PIDs and logs), `sim_out/` (truth, faults, dry-run JSONL).

## 2. Run everything in five commands

```sh
make setup                                                               # 1. .venv with pinned deps (uv)
make demo                                                                # 2. demo DB + API :8000 + 60-day simulated backfill + dashboard :8501
make test && make lint                                                   # 3. unit tests and ruff
make test-e2e                                                            # 4. real API + real simulator end to end
make stop                                                                # 5. stop the demo (safe when idle)
```

Day-to-day: `make api` (foreground), `make sim ARGS="--scenario sensor_faults --days 14"`,
`make dash`, `make reset-db` (drops and recreates tables behind `DB_URL`). `make setup` copies
`.env.example` to `.env` when missing; `API_KEY` is required (the API refuses to start without it)
and must be changed before the API is exposed beyond localhost.

## 3. How to add things

**A channel** (owner A; it is a contract change: bump `schema_version` and tell the other two):
1. `contract/channels.yaml`: add the entry (`unit`, `default_kind`, `valid_min`, `valid_max`,
   `max_step_15min` or null, `stuck_after` or null, `description`).
2. `CLAUDE.md` section 5 table and `contract/CONTRACT.md`: mirror it.
3. `make reset-db` (the `channels` table is re-seeded from the YAML; no code change needed: the
   Pydantic validator, QC and `/channels` all read the registry).
4. If the simulator should emit it: `sim/build.py` (`sensor_readings`, `external_readings`,
   `analyzer_readings` or `label_posts`) and `sim/physics.py` for its series.
5. Tests: `tests/db/test_schema_and_seed.py::test_channels_yaml_and_seeded_table_agree` already
   covers the seed; add a QC test in `tests/api/test_qc.py` if the channel has special rules.

**A node**: `config/reach.yaml` (`node_id`, `name`, `role`, `lat`, `lon`, `river_km`, `notes`,
mark unknowns `PLACEHOLDER`), then `make reset-db` or restart the API (seeding is an idempotent
upsert at startup). The simulator decides what a node emits by its `role`
(`intake`, `downstream`, `bench` = sensors; `effluent_input` = grab labels only). Unknown
`node_id`s in `/ingest` are auto-created with role `unknown`.

**A simulator scenario**: add a key under `scenarios:` in `sim/scenarios.yaml` overriding any
`defaults` (deep merge). Faults are the `faults:` map; parameters per fault type are documented in
`sim/README.md`. Then `make sim ARGS="--scenario <name> --days 7 --dry-run"` to inspect
`sim_out/<name>_seed1.jsonl` before posting. `tests/sim/test_physics.py::test_all_scenarios_load`
must be updated with the new name.

## 4. Simulator assumptions and measured limitations

The simulator's physics are **our assumptions, not Rio Grande facts** (`sim/README.md`, section
"Assumptions of ours, not Rio Grande facts", lists every equation and parameter). The only number
taken from our documents is the 2.82 mg/L as P effluent total phosphorus used in `effluent_step`.
Everything the simulator sends is `source='simulator'` with `meta.emulates`; the dashboard shows the
"SIMULATED DATA - pipeline validation only" banner whenever such rows are displayed.

Measured on 2026-11-03 (commands in `docs/ASSUMPTIONS.md`):

| what | result |
|---|---|
| ingest of 5,000 readings in one batch (`scripts/bench_p1.py`) | 1.106 s |
| fetch of 100,000 stored rows resampled to 1h | 0.463 s (25,000 buckets) |
| fetch of 100,000 raw rows | 0.430 s |
| 90-day `summer_bloom` backfill over HTTP, 15-min step | 155,292 readings in about 18 s, 0 rejected |
| `make demo` end to end (60 days, 103,524 readings) | 12.7 s on a fresh clone |
| QC on `sensor_faults`, 30 days, seed 1 (`scripts/qc_detection_rates.py`) | spikes: 49 injected, 42 flagged for any reason (86%), 19 with reason `spike` (39%); stuck: 9 episodes, 144 readings, 65 flagged `stuck` (45%), 5 of 9 episodes detected (56%); turbidity drift: 2,696 drifted readings, 5 flagged (0%) |

What those QC numbers mean (limitations of the foundation, not of the simulator):
- **The spike rule is blind on hourly channels.** The allowed step scales with the gap
  (`max_step_15min x gap/15 min`), so an hourly TDS read may jump 800 ppm unflagged. Spikes smaller
  than 4x the 15-min step on hourly channels are never caught. Fix candidates: cap the scaling
  (e.g. at 2x) or give hourly channels their own step in `channels.yaml`.
- **The stuck rule needs 12 identical values.** A 6 h stuck period on a 15-min channel flags at most
  13 of 24 readings (54%); on an hourly channel it is never flagged. Consider a duration-based rule.
- **There is no drift rule.** Slow turbidity drift (the known 8 h failure mode) is invisible to QC.
  The weekly cleaning event in the simulator exists so a future drift detector can be tested.
- Negative spikes that leave the valid range are flagged `out_of_range`, not `spike` (correct but
  worth knowing when reading the counts).
- The simulator does not clip sensor outputs to their physical range after a fault, so a fault can
  produce a negative NTU; real hardware would clip at 0 and such a spike would go unflagged.

Not verified: screenshots of the dashboard (Playwright not installed); the GitHub Actions workflow
(no remote); `--mode live` beyond a short manual run; behavior on Windows/WSL.

## 5. Contract and API as built, and deviations from CLAUDE.md

Contract v1 is implemented as written in CLAUDE.md section 5, plus these decisions:
- `IngestEnvelope` (envelope only) lets `/ingest` validate readings one by one and store the valid
  ones; the strict `IngestMessage` rejects a batch with a duplicate key. `/ingest` rejects the later
  duplicate with reason `duplicate_in_batch` and keeps the first.
- Unit string `mg/L` for both `phosphate_po4` (as PO4) and `total_phosphorus` (as P); the channel
  name carries the basis. CONTRACT.md and channels.yaml say so.
- `node_id` must match `^[a-z0-9][a-z0-9_-]{0,63}$`.
- `/labels` accepts an optional `meta` object (so the simulator can set `meta.emulates`); `node_id`
  follows the same slug pattern as `/ingest`.
- `channels.yaml` carries two QC fields the `channels` table does not (`stuck_after`,
  `saturation_min`); `/channels` returns both.
- `/nodes` returns `last_seen` (newest reading `ts`) and `last_received_at` (newest `received_at`).
- `/observations` resampling runs in pandas on SQL-filtered rows; `kind=label` with `resample` is
  400; the 200,000-row limit is checked with `COUNT` before loading; buckets are left-closed and
  left-labeled per (node, channel, kind, source, unit).

**Deviations:**
1. **Python >= 3.12 instead of >= 3.11.** Exact pins to the newest versions (numpy 2.5.4, pandas
   3.1.0) do not support 3.11, and the prompt required exact pins. `CLAUDE.md` section 3 was updated
   to say ">= 3.12" so the rulebook matches the lock file; uv installs 3.13 automatically.
2. Only two dashboard pages exist (Overview, Live signals), as the Phase 3 prompt asked; the other
   pages of section 10 are future work for C.
3. `make reset-db` drops and recreates tables instead of deleting the SQLite file (same result,
   works for any `DB_URL`).

## 6. Suggested split of the next phases

**A (backend, alerts, firmware)**, branch `feat/p4-alerts` then `feat/p6-firmware`:
- `alerts/`: rule engine (hysteresis, min consecutive evaluations, one open alert per rule and
  node, auto-resolve), notifiers (alerts table, log file, Telegram), `GET /alerts`,
  `POST /alerts/{id}/ack`, `GET /alerts/threshold-suggestion`, `GET /actuator/bench-pump`,
  `POST /admin/jobs/alerts`. Rules in `config/alerts.yaml` with PLACEHOLDER thresholds.
- `connectors/`: IBWC and weather CSV exports -> `/ingest` (`kind=external`, `meta.original_unit`).
- QC improvements listed in section 4 (spike scaling cap, duration-based stuck rule, drift rule).
- `firmware/`: PlatformIO project speaking the contract; the simulator's `ph_settling` flagging and
  buffered late upload are the reference behavior.
- Prerequisites from the foundation: `api/ingest.py` (reuse `ingest_readings` for connectors),
  `db/models.py` (`alerts` table exists), `/nodes.minutes_since_seen` for `sensor_silent`.

**B (ML)**, branch `feat/p4-ml`:
- `make setup-ml`; `ml/tasks/*.yaml`, features with `closed='left'`, walk-forward splits, baselines
  (persistence always), ridge -> RandomForest -> LightGBM, conformal intervals, registry and
  promotion rule, `run_forecast` writing champion and `persistence-v1`, scorecard join,
  `api/routers/ml.py` (`/models`, `/predictions`, `/scorecard`, `/label-coverage`,
  `POST /admin/jobs/{forecast,retrain,promote}`).
- **Prerequisite a person must do:** download the CRP Excel files (stations 13202, 13200, 13196,
  13189) from the IBWC site into `data/raw/crp/` (gitignored). Nothing in the repo can fetch them.
- From the foundation: `GET /observations` with `min_quality=good`, `resample=1D`, and
  `kind=label`; the simulated tasks use `make sim ARGS="--scenario summer_bloom --days 90 --dense-labels"`
  and `sim_out/truth.csv` for pipeline validation only. `models` and `predictions` tables exist with
  the unique keys of CLAUDE.md section 6.

**C (dashboard)**, branch `feat/p5-dashboard`:
- Pages Forecast, Model lab, Scorecard, Reach view (`dashboard/twin/massbalance.py` with the
  CLAUDE.md test numbers 0.1819 and 0.0524), Scenario, Admin (label entry via `/labels` and
  `/labels/csv`, job buttons). Keep every page API-only and working on an empty database; keep the
  banner logic (`dashboard/helpers.has_simulator_rows`) on every page.
- From the foundation: `dashboard/api_client.py` (add methods per new endpoint), `ui.py` sidebar,
  `tests/dashboard/conftest.py` (real API fixtures for AppTest).

## 7. Open questions for the humans

1. Physical node locations and whether the two-site design (intake + downstream) is final; every
   coordinate in `config/reach.yaml` is a placeholder.
2. Should the spike rule's gap scaling be capped (see section 4)? It decides whether hourly TDS
   spikes are ever caught.
3. Is `stuck_after: 12` identical readings the right definition, or should it be a duration?
4. Should the simulator's effluent node carry sensors, or stay labels-only as now?
5. Does leak detection (pressure transducers) stay in scope? `pressure` is in the registry but
   nothing produces it.
6. `API_KEY` is a single shared key; is that acceptable until Nov 20?
7. Who sets up the GitHub remote so CI runs, and who downloads the CRP files for B?
