# Assumptions and decisions

Decisions made during the foundation build (Phases 0 to 3) where the prompt or CLAUDE.md left a
choice. Each entry: the decision, the reason. Blockers are listed at the end.

## Environment (Step 0, recorded 2026-11-03)

- **Python 3.13.7** (Homebrew) with **uv 0.12.17**; git 2.51.0; make and curl present. Playwright
  is not installed, so no dashboard screenshots are taken.
- Resolved and pinned versions (from `uv lock` on 2026-11-03): fastapi 0.162.0, uvicorn 0.54.0,
  pydantic 2.14.2, sqlalchemy 2.1.7, pandas 3.1.0, numpy 2.5.4, pyyaml 6.0.3, httpx 0.28.1,
  python-multipart 0.0.33, streamlit 1.66.1, plotly 7.1.0, pytest 9.2.0, ruff 0.17.3; ML extra
  (declared, not installed): scikit-learn 1.9.1, lightgbm 4.7.0, shap 0.54.1, joblib 1.6.0,
  openpyxl 3.1.5. Lock file: `uv.lock`.

## Phase 0

- **`requires-python = ">=3.12"`, not `>=3.11`.** CLAUDE.md section 3 says Python >= 3.11, but the
  prompt also requires pinning every dependency to the exact newest installed version, and the
  newest numpy (2.5.4) and pandas (3.1.0) do not support 3.11. Exact pins and a 3.11 floor cannot
  both hold. Teammates on older Pythons run `uv python install 3.13` (uv does this automatically
  from `uv.lock`). Deviation flagged in the handoff; CLAUDE.md section 3 should say 3.12.
- **Unit string for phosphorus channels is `mg/L`** for both `phosphate_po4` (as PO4) and
  `total_phosphorus` (as P). The channel name carries the basis; descriptions and CONTRACT.md spell
  it out. Reason: HI713 CSV uploads and connectors write plain `mg/L`; a suffix in the unit string
  would be easy to get wrong and buys nothing the channel name does not already say.
- **`stuck_after: 12` only on continuous node-sensor channels** (water_temp, ph, tds, turbidity,
  the three raw volt channels, pressure). Not on flow_rate (0 while idle is normal), labels,
  external series (constant discharge or zero precip is normal) or analyzer channels (90-minute
  cycles). Reason: avoid false `stuck` flags on channels where constancy is expected.
- **Per-reading validation in the API uses two models.** `IngestMessage` is strict (whole message,
  used for the contract examples and by well-behaved clients). `IngestEnvelope` validates only the
  envelope so `/ingest` can validate readings one by one and store the valid ones, as the prompt
  requires. A duplicate key inside one batch is invalid for `IngestMessage`; `/ingest` rejects the
  later occurrence with reason `duplicate_in_batch` and keeps the first.
- **Timestamps are stored as naive UTC in SQLite and re-attached as UTC on read** through a
  `UTCDateTime` type decorator that refuses naive input. Reason: SQLite has no timezone type; the
  decorator enforces "UTC everywhere" at the database boundary.
- **`node_id` pattern** `^[a-z0-9][a-z0-9_-]{0,63}$` (lowercase slug). Not in CLAUDE.md; chosen so
  node ids are safe in URLs, file names and YAML keys.
- **`make reset-db` drops and recreates tables** rather than deleting the database file. Same
  effect for a SQLite file, and it also works for any other `DB_URL`.
- **Health response** adds `schema_version: 1` next to `version`, `db_ok`, `utc_now`.
- **Example files use October 2026 timestamps** (not the 2026-11-10 of the CLAUDE.md example)
  because today is 2026-11-03 and the contract rejects timestamps more than 10 minutes ahead.
- The `models`, `predictions` and `alerts` tables are created now so the schema is complete, but no
  code writes to them in the foundation.

## Phase 1

- **Spike threshold scaling has a floor at the 15-minute step.** Allowed change =
  `max_step_15min * max(gap / 15 min, 1)`. For gaps shorter than 15 min the 15-min step applies
  unchanged (otherwise 1-minute data would flag normal noise); for longer gaps it grows linearly.
- **Spike reference = nearest earlier reading with quality `good`.** A spike is flagged once; the
  return to normal is compared against the last good value and is not flagged as a second spike.
  Readings the node sent as suspect or bad are never a reference.
- **Stuck check counts identical values regardless of quality**, over the last 24 stored rows plus
  the batch; it fires from the `stuck_after`-th identical value onward (the first 11 stay good).
- **QC order:** out_of_range, saturated (tds >= 950), spike, stuck. First hit wins; one reason per
  row. Spike and stuck apply to `kind=measurement` only; range checks apply to every kind.
- **QC reason bookkeeping:** `meta.reason` is set when absent. If the node already sent a
  `reason` (for example `ph_settling`) it is kept and QC's reason goes to `meta.qc_reason`.
- **Multi-row `INSERT ... ON CONFLICT DO NOTHING` in chunks of 500 rows**; duplicates = rows sent
  minus `rowcount`, never a SELECT first.
- **`/nodes.last_seen` is the newest reading `ts`** (data freshness, what `sensor_silent` needs);
  `last_received_at` is also returned for "when did the API last hear from it".
- **`/observations` resampling runs in pandas** on the filtered rows (SQL `WHERE`, never the whole
  table). The 200,000-row hard limit is checked with a `COUNT` before loading, for raw and resampled
  queries alike. Resampling applies to `measurement` (default) or `external` rows; `kind=label`
  with `resample` is a 400. Buckets are left-closed, left-labeled, grouped per
  (node_id, channel, kind, source, unit).
- **`min_quality` semantics:** `good` returns only good rows, `suspect` good and suspect, `bad`
  (default) everything. It is a filter on stored quality, applied before resampling.
- **CSV labels:** blank `channel` -> `phosphate_po4`, blank `source` -> `HI713`, blank `unit` ->
  the channel's canonical unit. Line numbers are 1-based with the header as line 1.
- **Measured on 2026-11-03 (scripts/bench_p1.py, in-process TestClient, SQLite file, Apple
  Silicon laptop):** ingest of 5,000 readings in one batch 1.106 s; fetch of 100,000 stored rows
  resampled to 1h 0.463 s (25,000 buckets); the same 100,000 rows raw 0.430 s. Ingest cost is
  dominated by per-reading validation and QC in Python (about 0.2 ms per reading).

## Phase 2 (simulator)

- **Every scenario parameter is an assumption**, chosen to exercise the pipeline (seasonality,
  storms, dilution, effluent step, a bloom). The only number from our documents is the 2.82 mg/L
  as P effluent total phosphorus in `effluent_step` (2020 permit application, CLAUDE.md 11).
  `sim/README.md` lists every equation under "Assumptions of ours, not Rio Grande facts".
- **Node roles decide what is simulated.** `intake` and `downstream` are sensor nodes;
  `intake` also carries the external series (`discharge` emulating IBWC, `air_temp`/`precip`
  emulating weather) because the contract needs a `node_id` and the gauge is near the intake
  station; `downstream` hosts the analyzer; `effluent_input` only gets HI713-style grab-sample
  labels (it is a source term in the physics, not a sensor site); `bench` is a still bucket with
  sensors. Reason: no sensors are planned at the effluent, and `/nodes` should still show the
  effluent node as sampled.
- **Baseline seasonal cycle is damped** (`season_amp` 4 degC) so `baseline` never blooms whatever
  the start date, and `summer_bloom` uses a hot mean so it blooms whatever the start date. The
  bloom test (`chlorophyll_a_true > 10` in `summer_bloom`, never in `baseline`) must hold for any
  `--start-date`.
- **Spike magnitude is `step_multiple x max_step_15min` with either sign** and no clipping to the
  physical range: a negative turbidity spike yields a negative NTU reading, which QC flags as
  `out_of_range` rather than `spike`. Both count as "caught" in the detection report.
- **Labels are posted one at a time to `/labels`** (not bundled into `/ingest`) so that endpoint
  is exercised by the simulator; `LabelIn` gained an optional `meta` field so `meta.emulates`
  survives. Grab-sample times are snapped to the step grid so `truth.csv` has a matching row;
  two samples landing on the same step collapse into one (unique key).
- **`sent_at` is simulated time** (end of the batch, or outage end + 1 min), not wall-clock, so
  dry-run output is byte-identical for the same seed. Nothing validates `sent_at` against now.
- **Live mode is a paced replay** of the generated horizon (one batch per node per step, wall-clock
  time between batches = step / `--speedup`). With `--start-date` today and `--speedup 1` it
  tracks real time; with the default start it replays history.
- **Fault `episodes` are per targeted node**: `stuck: {episodes: 3}` without `node_role` gives 3
  episodes on each of the 3 sensor nodes (9 in total).
- **`--out-dir` option added** (default `sim_out/`) so tests can write to a temporary directory.
- **Measured QC detection (2026-11-03, `scripts/qc_detection_rates.py --days 30`, seed 1,
  scenario `sensor_faults`, 51,604 readings accepted, 118 duplicates from outage replays, 0
  rejected):**
  - spikes: 49 injected; 42 flagged suspect for any reason (86%); 19 flagged with reason
    `spike` (39%). The gap is explained, not guessed: 8 negative turbidity spikes left the valid
    range (`out_of_range`), 15 TDS spikes went `out_of_range`/`saturated`, and 7 TDS spikes were
    missed because TDS is read hourly and the spike rule scales the allowed step with the gap
    (allowed = 200 ppm x 4 = 800 ppm per hour), so a 300 to 800 ppm spike passes.
  - stuck: 9 episodes of 6 h, 144 readings; 65 readings flagged `stuck` (45%); 5 of 9 episodes had
    at least one flag (56%). The 4 undetected episodes are on the hourly TDS channel: 6 h of hourly
    reads is 6 identical values, below `stuck_after: 12`. For 15-min channels the rule fires from the
    12th identical reading, so at most (24 - 11)/24 = 54% of a 6 h episode can be flagged.
  - turbidity drift: 2,696 drifted readings, 5 flagged (0%). QC has no drift rule; expected.
  - Test floors (`tests/sim/test_backfill.py`): any-reason spike rate >= 0.60, `spike`-reason rate
    >= 0.25, stuck readings >= 0.30, stuck episodes >= 0.40. Chosen at roughly half to two thirds of
    the measured values so the test fails if QC is disabled or broken, not on seed variation.
- **90-day backfill unit test uses `--step-min 60`** (about 48k readings, about 8 s) to keep `make
  test` under a minute; the 15-minute version runs through `make test-e2e`.

## Phase 3 (dashboard v0 and demo)

- **Two pages only** (Overview, Live signals), as the prompt asks; the other pages of CLAUDE.md
  section 10 (Forecast, Model lab, Scorecard, Reach view, Scenario, Admin) belong to later phases.
- **Views are standalone scripts** in `dashboard/views/` wired together by `st.navigation` in
  `dashboard/app.py`. Reason: `AppTest.from_file` can then run each view directly, and
  `set_page_config` is guarded so a view also runs on its own. The folder is not called `pages/`
  on purpose (that name triggers Streamlit's legacy automatic multipage mode).
- **Auto-refresh** uses `st.fragment(run_every=15)` around the data-dependent part of each view;
  the sidebar toggle switches to a plain render. Widgets (node and channel pickers) sit outside the
  fragment so they are not re-created every tick.
- **Overview latest values** come from a raw `/observations` query of the last 24 h per node
  (`kind=measurement`), newest row per channel; nodes without measurements say so.
- **Live signals** fetch one API call per (node, channel) plus one label query per node, using the
  API's `resample` with `agg=mean`; labels are never resampled. Resampled points carry `n` in hover.
- **Banner logic** is a pure function (`has_simulator_rows`) fed with the sources of every row that
  is displayed (measurements and labels), rendered into a placeholder at the top after the data
  arrives, so it is visible whatever the page shows below.
- **API-down state** is detected in the sidebar health probe and at every fetch; pages show an
  error box with the API URL and the `make api` hint, never a traceback.
- **`make demo` refuses to start when something already answers on the API URL** and `make stop`
  kills the whole process tree (`uv run` wraps the real server as a child). Reason: during the
  Phase 3 gate a leftover API from an earlier step still held port 8000, the demo API silently
  failed to bind and the backfill went into the wrong database. The guard makes that loud.
- **`DEMO_DAYS` make variable** (default 60) so a shorter demo is possible on slow machines.
- **Screenshots not taken**: Playwright is not installed on this machine and the prompt forbids
  installing browsers.
- **Measured (2026-11-03):** `make demo` end to end 12.8 s on this laptop (reset, API start,
  60-day backfill of 103,524 readings, Streamlit start); dashboard answers HTTP 200 within a second
  of start. AppTest smoke tests (2 pages x empty / populated / API-down + 2 interaction tests)
  run in about 3 s because the populated server holds only 3 days of 30-minute data.

## Integration check (2026-11-03)

- Fresh `git clone -b feat/foundation-p0-p3` into a temporary directory, following only the README
  quickstart. Measured with `/usr/bin/time -p` (wall seconds): `make setup` 0.97 (uv cache was warm;
  a cold machine downloads about 80 packages first), `make test` 51.82 (pytest itself reported 29.85 s;
  the rest is the first `uv run` building the editable package and importing pandas/streamlit),
  `make lint` 0.39, `make demo` 12.70 (reset, API start, 60-day backfill of 103,524 readings,
  Streamlit start); `/health` ok, `/nodes` showed 4 nodes with rows, dashboard HTTP 200,
  `make stop` stopped both. No README fix was needed: it worked the first time.

## Self-review (2026-11-03)

An independent reviewer (a second Claude session reading a clean clone against CLAUDE.md) reported
20 findings; none about fabricated numbers, scope-fence violations or committed secrets. Fixed:
- `/labels` and `/labels/csv` did not validate `node_id` against the contract slug pattern and
  auto-created nodes that `/ingest` would reject (regression test `test_label_node_id_must_be_a_slug`).
- The development key literal lived in `api/settings.py` and `sim/run.py` as a default. Removed:
  `API_KEY` is now required (the API refuses to start without it, the simulator asks for
  `--api-key`), `make setup` creates `.env` from `.env.example` when missing, and a test asserts
  the literal appears in no `.py` file.
- Vacuous or weak tests: default-start test now checks `resolve_start`; the PO4/P consistency test
  counts its checks; the dashboard UTC toggle test requires a chart; a tautological assert removed;
  the bloom-label assertion filters `chlorophyll_a`; QC floors tightened to a few points under the
  deterministic measured values (0.80 / 0.35 / 0.40 / 0.50) with the rationale corrected.
- Live signals plotted every label channel on the primary axis regardless of the picked channels;
  labels now follow the picked channels (label-only channels are pickable) and the axis title lists
  the units of what is drawn.
- Diurnal peaks were at 15:00 to 16:00 UTC (mid-morning in Laredo); now `diurnal_peak_hour_utc`
  parameters (21 and 22 UTC, mid-afternoon in America/Chicago). QC detection rates re-measured:
  unchanged (86% / 39% / 45% / 56%), 60-day count unchanged (103,524).
- TDS saturation threshold moved from Python into `channels.yaml` (`saturation_min: 950`).
- Dead code removed (`Settings.tz_display`, `RIVER_ROLES`, `Batch.n_readings`, `Point.reason`,
  `_Validated.qc_reason`); `TZ_DISPLAY` now sets the dashboard's default time zone.
- Simulated discharge no longer claims `original_unit: ft3/s` (no conversion happened).
- `qc_reason` documented in CONTRACT.md; stale test-file references fixed; deprecated
  `use_container_width` replaced.

Not changed, on purpose:
- CLAUDE.md section 5 still writes the phosphorus units as `mg/L (as PO4)` / `mg/L (as P)` while
  the contract uses `mg/L` with the basis in the channel name (see Phase 0). A rulebook wording
  change needs the teammates' PR; listed as a deviation in HANDOFF.md.
- The `channels` table has no `stuck_after` or `saturation_min` column because CLAUDE.md section 6
  fixes its columns; `/channels` adds both from the YAML. A schema PR can add them later.
- `DASHBOARD_FIXTURES` stays in CLAUDE.md section 3 (the prompt excluded it from `.env.example`).
- `set_page_config` is still wrapped in `contextlib.suppress(StreamlitAPIException)` so a view runs
  both standalone (tests) and under `st.navigation`; the only exception that call raises in
  practice is the "already set" one.

Break-it checks (each: broke the code, ran the matching test, saw it fail, `git checkout -- <file>`,
saw it pass): (a) ingest idempotency: `ON CONFLICT DO NOTHING` -> `DO UPDATE`, test
`test_resending_a_batch_adds_zero_rows` failed (accepted/duplicates differ), restored, passed. A
first attempt (`INSERT OR REPLACE` prefix) did not change behavior, so the stronger break was used.
(b) QC never upgrades: `worst_quality(sent, suspect)` -> `suspect`, `test_qc_never_upgrades_quality`
failed, restored, passed. (c) naive timestamps: validator returned the value as UTC instead of
raising, `test_naive_timestamp_rejected_with_clear_message` failed, restored, passed. (d) resample
worst quality: bucket aggregation `max` -> `min`, `test_resample_matches_pandas_reference` failed,
restored, passed. (e) simulator determinism: `SeedSequence(seed)` -> `SeedSequence()`,
`test_same_seed_same_output_different_seed_differs` failed, restored, passed.

## Blockers

None so far.

## Phase 4a: real CRP data (recorded 2026-11-09)

Decisions for `ml/data/crp.py` (inspect, clean, manifest, verify). Scope for this task was set by
the repository owner: `ml/data/`, `tests/ml/`, `docs/crp_schema.md` and this file (append only).

- **Sample time convention.** CRP dates have no time of day. Each is stored as that date at
  **18:00:00 UTC** (about midday US Central; the real sample time is unknown) with
  `meta.date_only=true`. Set in `ml/data/crp_mapping.yaml` (`date.stored_time_utc`).
- **Two-digit years:** yy >= 70 is 19yy, otherwise 20yy (PLACEHOLDER pivot in the mapping; the
  three files span 1995 to 2026).
- **Real data supports "predict the next sample", not a 7-day forecaster.** Nothing is
  interpolated, resampled or synthesized between samples; every row has `source='CRP'`.
- **Units.** Specific conductance: the header says S/cm, values are stored as uS/cm (middle of the
  data is roughly 600 to 1300; see `docs/crp_schema.md`). Flow: ft3/s x 0.0283168 = m3/s, original
  unit in `meta.original_unit`, original number in `value_raw`. Ortho-phosphorus stays
  `crp_ortho_phosphorus` with basis PLACEHOLDER (not mapped to `phosphate_po4`). Kjeldahl-N and
  the two bacteria columns have unit PLACEHOLDER. Lab TDS is `crp_tds_lab`, not the sensor `tds`.
- **Kind.** `chlorophyll_a` and `total_phosphorus` are `label`; every other CRP column is
  `measurement`, including `discharge` and `air_temp` (the contract's default kind for those two
  channels is `external`, which describes IBWC gauge and weather feeds, not a grab-sample reading).
- **Censored values.** Left-censored (`<x`): `value = x / 2`, `detection_limit = x`,
  `censored=left`. Right-censored (`>x`): `value = x`, `detection_limit = x`, `censored=right`
  (the brief defined only the left case). `value_raw` always keeps the original text. A `<10`
  chlorophyll label therefore counts as below the 10 ug/L event level.
- **Screening rules** (`ml/data/crp_qc.yaml`, all PLACEHOLDER heuristics): range checks compare the
  number as written in the file and apply to plain numeric cells only, not censored ones. A flag
  sets `quality='suspect'` and keeps the value; `bad` is never assigned by this task.
- **Out-of-sequence dates:** a row is quarantined when its date lies outside
  [previous row, next row] while previous <= next. The first and last row of a file have one
  neighbour and are never quarantined by this rule; any remaining order break is listed in the
  report. No year is ever guessed.
- **Duplicate blocks** are compared within one station only; the block on the later date is
  flagged. A quarantined out-of-sequence row is still compared, for the report.
- **Same-date merge:** the first row's value wins. A different value on a later row goes to
  `crp_quarantine.csv` (`conflicting_duplicate`); an identical value goes there too
  (`duplicate_same_value`) so that every parsed cell is accounted for exactly once.
- **Extra output columns** beyond the brief, agreed with the owner: `crp_long.csv` has a trailing
  `meta` column (JSON: `date_only`, `original_unit`); `crp_quarantine.csv` has `column`,
  `value_raw` and `n_cells` so a single quarantined value and the conservation check are
  expressible.
- **Text cells on a quarantined row** are recorded in `crp_notes.csv` like any other text cell;
  the quarantine entry counts only its numeric and censored cells.
- **Corrections** (`ml/data/crp_corrections.yaml`, empty) are human decisions, not QC:
  `confirm_ok` and `replace` set `quality='good'` with `flag_reason` `confirmed_ok` / `corrected`
  (`value_raw` keeps the file text); `drop` moves the value to the quarantine file. An entry
  without evidence, or one that matches no value, stops the run.
- **Profile** (`data/clean/crp_profile.json`): `quality='good'` rows only; censored values enter at
  their stored value; "pooled" is all stations together and pooled gaps are the per-station gaps
  concatenated; percentiles use linear interpolation between order statistics (a statistic of the
  observed values, not interpolation between samples); a cell with n < 5 is null.
- **File-name check** ignores hidden dot-files (for example `.DS_Store`) in `data/raw/crp/`.
- **xlrd not needed:** `data/raw/crp/` holds only CSV files, so `pyproject.toml` is unchanged.
  The `.xlsx` path uses openpyxl (ML extra); its test is skipped when openpyxl is not installed.
- **`.gitignore` line `data/` also matches `ml/data/`.** The owner chose not to edit `.gitignore`
  in this task, so the `ml/data/` files are committed with `git add -f`. Consequences until the
  line becomes `/data/`: new files in `ml/data/` need `git add -f`, and `make lint` (ruff respects
  `.gitignore`) does not check `ml/data/*.py`; run `uv run ruff check ml/data/*.py` explicitly.
- **Reference count not reproduced:** the brief expected 15 of 21 rows dated 2025 or later at
  15816 without field readings. The file has 11 rows dated 2025 or later (lines 171 to 181), 5 of
  them without numeric water temp, pH or flow. Neither side was adjusted.

Blockers (Phase 4a): none.

## Phase 4b: ML harness and the CRP tasks (2026-11-10)

Branch `feat/p4b-ml-harness`, created from `feat/p4a-crp-data` (4a was not merged).

- **Dependencies.** The `ml` extra was not installed in the shared environment; `uv sync --extra ml`
  installed it. Two exact pins were added to the `ml` extra in `pyproject.toml`: `scipy==1.18.1`
  (the version scikit-learn 1.9.1 already resolved to) and `matplotlib==3.11.2`. On macOS LightGBM
  also needs the system OpenMP runtime: `brew install libomp` (not a Python package, so it cannot
  be pinned in `pyproject.toml`; it belongs in the README setup notes, which this task may not edit).
- **Tests that need the `ml` extra** (`tests/ml/test_models.py`, `test_drills.py`, `test_run.py`)
  are skipped with `pytest.importorskip` when scikit-learn, LightGBM or matplotlib is missing, as
  4a did for openpyxl. After `make setup` alone they skip; after `make setup-ml` they run.
- **Task specs use `extends`.** `ml/tasks/_crp_base.yaml` (marked `abstract`) holds the shared
  settings; a task file merges over it (dicts merge, lists and scalars are replaced). A task's
  `fast:` block is merged over it only for `--fast`. This is loader behaviour, not task logic.
- **Seasonal climatology widens CLAUDE.md's "same month".** It is the mean of the training
  targets whose calendar month is within plus or minus one month of the test month (December and
  January are adjacent), because there are only about two or three labels per station and
  calendar month. With fewer than 3 training labels in the window it is the mean of all training
  targets. It is pooled over the task's stations (not per station) for the same reason.
- **Climatology for regression targets** is the mean on the transformed (log1p) scale,
  back-transformed, so it is on the same footing as the models, which are fitted on that scale.
- **Persistence uses the latest label known at issue time**, including a label that falls inside
  the test year or inside the 14-day gap: "the last sample at this station, carried forward". The
  gap restricts what a model may be trained on, not what the baseline may look at. The same holds
  for the `prev_label_*` features. This assumes the previous sample's lab result is back before
  the next visit (visits are 56 to 112 days apart); the lab turnaround time is a PLACEHOLDER.
- **Labels before `start` are not used at all**, not even as a "previous label".
- **Total phosphorus targets use log1p** ("regression on the log scale" in the brief; CLAUDE.md
  section 8 says log1p for concentrations). Feature transforms follow the brief: `log` for
  specific conductance, total phosphorus and TSS; `log1p` for discharge and the previous label.
- **Censored values.** A left-censored target enters the regression at detection limit / 2 with
  a `censored` flag, and metrics are reported on all labels and on numeric-only labels. For the
  event target, `<x` with x <= threshold is a negative and `<x` with x > threshold is dropped
  (counted in the report). A right-censored target is dropped from both targets and counted
  (the brief defined this for chlorophyll; total phosphorus is treated the same way). A censored
  value in a *feature* channel (for example total phosphorus `<0.05` in `lab_same_visit`) enters
  at its stored value, detection limit / 2.
- **Regression predictions are clipped at zero** concentration before any metric is computed.
- **Final hold-out size** is `ceil(final_holdout_frac * distinct dates)` dates. The calendar year
  that contains the hold-out start is a partial walk-forward test fold (its dates before the
  hold-out start).
- **Missing-indicator columns** are decided per fold from the missing pattern of the training and
  test rows; the medians come from the training rows only. A feature that is entirely missing in
  a training fold is filled with 0 (its indicator carries the information).
- **Bootstrap intervals** are plain percentile intervals over 1000 resamples of distinct dates
  (seed 7). A metric that is undefined on a resample (PR-AUC without positives) is left out of
  that metric's percentiles; `n_boot_defined` in `metrics.json` says how many were used.
- **Verdict for event tasks** is decided by PR-AUC; the Brier difference and its interval are
  printed next to it and can disagree.
- **Shuffled-label drill.** Event: mean PR-AUC over 5 seeds against the base rate of the
  out-of-sample labels, margin 0.10 (PLACEHOLDER), exactly as in the brief. Regression (not
  specified in the brief): mean MAE skill against the training-mean predictor on the transformed
  scale, same margin. The first version used the training median; it was changed after the first
  full run because a squared-loss model fitted on shuffled targets estimates the training mean,
  and on these tasks the mean scores better than the median without any model (for example
  crp_chla_17410: MAE 0.873 against 0.990, log scale, ad hoc check), which raised flags.
- **The event rule of the shuffled-label drill raises false alarms on small samples.** PR-AUC of
  a ranking that ignores the labels is above the base rate when there are few labels (ad hoc
  check: 0.403 against a base rate of 0.368 at n = 68; 0.686 against 0.571 at n = 7), and five
  seeds are a noisy mean. The first full run printed LEAKAGE SUSPECTED on 5 of 7 tasks. A 30-seed
  ad hoc check found mean shuffled-label ROC-AUC at or below 0.5 for every model checked, so no
  leakage was found. The rule and its margin were NOT changed (that is the owner's decision to make); the report now
  prints the mean shuffled ROC-AUC and the label-blind expected PR-AUC next to every flag.
- **Leaky-feature guard** is the truncation check: `assert_no_future_dependence` raises when a
  feature changes after later rows are removed. The leaky feature (next sample's target) exists
  only inside `tests/ml/test_drills.py`.
- **Conformal intervals** use the absolute walk-forward residuals on the transformed scale and
  are applied to the hold-out predictions; coverage is counted there. Fewer than 30 residuals
  (PLACEHOLDER) prints a note.
- **Permutation importance** is the drop in pooled walk-forward PR-AUC (event) or the rise in
  log-scale MAE (regression) when one raw feature is shuffled within each fold's test rows,
  5 repeats, nothing refit. SHAP was not built (first item on the cut list).
- **"Last two calendar years"** in the feature-availability table are the last two calendar years
  that have labels in the task (2025 and 2026 today), not the wall-clock date.
- **`--fast` runs** are written to `<UTC-date>-FAST/` so they never overwrite a real report.
- **`metrics.json` holds no timestamp**, so two runs on the same inputs are byte-identical; the
  UTC date is only in the folder name.
- **`ml/__init__.py` was added** (docstring only) so `ml` is a regular package.

Blockers (Phase 4b): none.
