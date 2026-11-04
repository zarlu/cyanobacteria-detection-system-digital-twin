# Cyanobacteria Detection System Digital Twin: rules for Claude Code

> Rulebook for every Claude Code session on this repository. Change it only through a pull request all three teammates have seen.

## 1. What we are building

Software for a senior-design "Cyanobacteria Detection System Digital Twin" (TAMIU) that watches the Rio Grande near Laredo for phosphorus and cyanobacteria (bloom and taste-and-odor) risk:

ESP32 nodes -> JSON over WiFi -> FastAPI -> SQLite -> ML models -> forecasts and alerts -> Streamlit "twin" dashboard

The hardware has not arrived. Until it does, a **simulator speaks the exact node contract** and everything is developed against it. Due **2026-11-20** (feature freeze **2026-11-15**): a trained, honestly backtested model plus a basic working loop: reading -> database -> model -> alert -> dashboard, with every prediction stored so later labels can score it.

Three developers, each running Claude Code on their own branch: **A** backend/integration/firmware, **B** ML, **C** dashboard. Phases 0 to 3 (the foundation) are built first by a single agent; the three developers are assigned afterwards.

## 2. Non-negotiable rules

1. **Honest numbers.** Never fabricate or round up results. Every metric in code output, docs or chat comes from a command that was actually run. Negative results get the same prominence and sentence structure as positive ones.
2. **No random splits for time-series data, ever.** Splits are chronological (walk-forward plus one final hold-out used once). A feature at issue time `t` uses only data with `ts <= t`.
3. **Every model result sits next to its baselines**: persistence always, seasonal climatology for monthly data. "Does it beat persistence?" is answered in words in every report.
4. **UTC everywhere.** Store and exchange UTC ISO-8601 only. Convert to America/Chicago only for display.
5. **Simulated data is always labeled.** `source='simulator'`, a dashboard banner, and reports that say "pipeline validation only, not river performance". The simulator's physics are our assumptions, not Rio Grande facts.
6. **Only `quality='good'` rows feed training and features** unless a task spec says otherwise. Server QC may downgrade quality (good -> suspect -> bad) and never upgrade it.
7. **No self-updating models.** Retraining is an explicit job. A candidate becomes champion only through the promotion rule in section 8. Every prediction is stored with `model_version` and `issued_at`.
8. **Idempotent writes.** Re-sending any message or file changes nothing.
9. **Units are explicit.** `phosphate_po4` is mg/L **as PO4** (what the Hanna HI713 reports). `total_phosphorus` is mg/L **as P** (what CRP data and permits report). 1 mg/L PO4 = 0.3261 mg/L P. Never mix them without converting.
10. **No invented local facts.** Anything about Laredo or the Rio Grande that is not in section 11 is marked `PLACEHOLDER` in config and docs.
11. **Config over code.** Channels, nodes, tasks, alert rules and scenarios live in YAML. No river or station names hard-coded in Python. This keeps a later "bring your own data" mode possible.

## 3. Stack and commands

- Python >= 3.12 (3.13 used for development; changed from 3.11 in the foundation build because the exact pins of numpy 2.5.4 and pandas 3.1.0 need 3.12), one shared environment (`pyproject.toml`, every dependency pinned to an exact version).
- FastAPI + Uvicorn, Pydantic v2, SQLAlchemy 2.0 (sync) on SQLite with WAL, no Alembic (`make reset-db` is the migration strategy).
- pandas, numpy, PyYAML, httpx, python-multipart, Streamlit, Plotly, pytest, ruff. The ML libraries (scikit-learn, LightGBM, SHAP, joblib, openpyxl) live in an optional `ml` extra installed by `make setup-ml`; they are needed from Phase 4.
- Firmware: PlatformIO (Arduino framework), board `esp32-s3-devkitc-1` (N16R8).
- `make` targets: `setup`, `setup-ml`, `test` (excludes the `e2e` marker), `test-e2e`, `lint`, `api`, `sim ARGS="..."`, `dash`, `demo`, `stop`, `reset-db`, `reproduce`.
- Environment (`.env`, never committed; `.env.example` is): `API_KEY`, `DB_URL` (default `sqlite:///data/twin.db`), `API_URL`, `TZ_DISPLAY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DASHBOARD_FIXTURES`.
- **Out of scope before Nov 20:** general upload or auto-ML mode, React, Postgres/Timescale, LSTM or other deep models, on-device ML, Docker/Kubernetes/Kafka.

## 4. Layout and ownership

| Path | Owner | Contents |
|---|---|---|
| `contract/` | A | Pydantic models, `channels.yaml`, `CONTRACT.md`, `examples/` |
| `db/` | A | SQLAlchemy models, init, seeding |
| `api/` | A | FastAPI app. **Exception:** `api/routers/ml.py` is owned by B |
| `alerts/` | A | rule engine, notifiers, threshold helper |
| `sim/` | A | simulator, `scenarios.yaml`, `faults.py` |
| `connectors/` | A | CSV exports (IBWC flow, weather) -> `/ingest` |
| `firmware/` | A | PlatformIO ESP32-S3 project |
| `ml/` | B | `tasks/*.yaml`, `data/`, features, splits, baselines, models, eval, registry, jobs, scorecard, `reports/` |
| `dashboard/` | C | Streamlit pages, `api_client.py`, `twin/` (reach view, mass balance), `fixtures/` |
| `config/` | A | `reach.yaml`, `alerts.yaml` (shared; changes via PR) |
| `docs/` | all | `ASSUMPTIONS.md`, `DRILLS.md`, `MODEL_REPORT.md`, `CLAIMS.md`, `DEMO.md` |
| `tests/` | each owner | `tests/<area>/` |
| `data/`, `artifacts/` | gitignored | raw files (`data/raw/crp/`, IBWC exports), `twin.db`, saved models |

Touch only your own paths. If you need a change in someone else's path or in `contract/`, stop and tell the human instead of making it. During the foundation build (Phases 0 to 3) one agent works in all paths except `ml/`, `firmware/`, `alerts/` and `connectors/`, which get README stubs only.

## 5. Data contract v1

Nodes POST one JSON message to `/ingest` (header `X-API-Key`):

```json
{
  "schema_version": 1,
  "node_id": "node-intake",
  "source": "sensor",
  "sent_at": "2026-11-10T15:04:05Z",
  "firmware": "0.1.0",
  "readings": [
    {"ts": "2026-11-10T15:04:00Z", "channel": "water_temp", "value": 27.4,
     "unit": "degC", "quality": "good", "kind": "measurement", "meta": {}}
  ]
}
```

- `source` at message level is the default; a reading may override it. Enums: **kind** = `measurement | label | external`; **source** = `sensor | analyzer | HI713 | CRP | IBWC | weather | manual | simulator`; **quality** = `good | suspect | bad`.
- `meta` is an optional object. Conventional keys: `reason` (`out_of_range`, `spike`, `stuck`, `saturated`, `ph_settling`, `ref_drift`, `led_low`, `sensor_error`), `cycle_id`, `is_reference_run`, `original_unit`, `emulates` (simulator only: which real source it imitates), `censored`, `sample_id`.
- `ts` must be UTC (`Z` or an explicit offset, normalized to UTC). Naive timestamps are rejected. Timestamps more than 10 minutes in the future are rejected. There is no limit on how old a reading may be (buffered uploads are normal). Truncate to whole seconds.
- `value` must be a finite number. `unit` must equal the channel's canonical unit in `contract/channels.yaml`.
- Uniqueness key: `(node_id, channel, ts, kind, source)`. A repeat is a duplicate, not an error.
- The analyzer's readings use the `node_id` of the station where it sits (`source='analyzer'`).

Channel registry (canonical; `contract/channels.yaml` is the source of truth, ranges are PLACEHOLDERs to tune on real data):

| channel | unit | default kind | valid range | max step per 15 min | notes |
|---|---|---|---|---|---|
| `water_temp` | degC | measurement | 0 to 45 | 3 | DS18B20 |
| `ph` | pH | measurement | 4 to 11 | 1.0 | DFRobot Gravity analog; see section 11 |
| `tds` | ppm | measurement | 0 to 1000 | 200 | sensor tops out at 1000; >= 950 flagged `saturated` |
| `turbidity` | NTU | measurement | 0 to 1000 | 200 | sensors on order; drifts after about 8 h |
| `ph_raw_v`, `tds_raw_v`, `turbidity_raw_v` | V | measurement | 0 to 5 | none | raw ADC volts for later recalibration |
| `flow_rate` | L/min | measurement | 0 to 30 | none | YF-S201 pipe flow, not river discharge; optional |
| `pressure` | psi | measurement | 0 to 100 | 30 | optional (leak-detection scope undecided) |
| `phosphate_po4` | mg/L (as PO4) | measurement | 0 to 5 | 1.0 | analyzer output (`source=analyzer`) or label (`source=HI713`, reads 0 to 2.5, +/-0.04 +/-4%) |
| `analyzer_i0`, `analyzer_i` | V | measurement | 0 to 5 | none | photodiode readings |
| `analyzer_absorbance` | AU | measurement | 0 to 3 | none | A = -log10(I/I0) |
| `analyzer_ref_drift` | AU | measurement | -1 to 1 | none | reference-run change vs previous reference run |
| `analyzer_led_level` | V | measurement | 0 to 5 | none | I0 on reference water |
| `chlorophyll_a` | ug/L | label | 0 to 500 | none | CRP monthly or quarterly |
| `total_phosphorus` | mg/L (as P) | label | 0 to 10 | none | CRP |
| `discharge` | m3/s | external | 0 to 5000 | 500 | IBWC; connector converts and stores `original_unit` |
| `air_temp` | degC | external | -10 to 50 | 10 | weather |
| `precip` | mm | external | 0 to 300 | none | per interval; interval in `meta.interval_min` |

## 6. Database (SQLite, SQLAlchemy models in `db/`)

- `nodes(node_id PK, name, role, lat, lon, river_km, notes)`; roles: `intake | downstream | effluent_input | bench | unknown`
- `channels(channel PK, unit, description, default_kind, valid_min, valid_max, max_step_15min)` seeded from `contract/channels.yaml`
- `observations(id, ts, node_id, channel, value, unit, quality, kind, source, meta_json, received_at)` with `UNIQUE(node_id, channel, ts, kind, source)` and an index on `(node_id, channel, ts)`
- `models(version PK, task, algorithm, target, horizon_h, features_json, train_start, train_end, train_sources, n_train, metrics_json, baseline_metrics_json, status, artifact_path, notes, created_at)`; status `candidate | champion | retired`; exactly one champion per task
- `predictions(id, issued_at, valid_at, horizon_h, node_id, target, yhat, lo, hi, p_event, model_version)` with `UNIQUE(model_version, node_id, target, issued_at, horizon_h)`. The baseline is stored as `model_version='persistence-v1'`
- `alerts(id, created_at, resolved_at, acknowledged_at, node_id, rule_id, level, message, p_event, model_version, status)`

## 7. API surface (v1)

| Method and path | Owner | Purpose |
|---|---|---|
| `GET /health` | A | version, db ok, UTC now |
| `POST /ingest` | A | validate, QC, idempotent insert; returns `{accepted, duplicates, rejected:[{index, reason}]}` |
| `GET /nodes`, `GET /channels` | A | nodes with `last_seen`, minutes silent, good/suspect/bad counts for the last hour; channel registry |
| `GET /reach` | A | `config/reach.yaml` (nodes in river order, effluent input) |
| `GET /observations` | A | filters `node_id, channel, kind, source, start, end, min_quality`; `resample` (15min, 1h or 1D) with `agg` (mean, median, last, max or min); columnar JSON; hard limit 200k rows |
| `POST /labels`, `POST /labels/csv` | A | single label; CSV `ts_utc,node_id,channel,value,unit,source,sample_id,notes` with per-line errors |
| `GET /models`, `GET /models/{version}/report` | B | registry; stored report and importance JSON |
| `GET /predictions`, `GET /scorecard`, `GET /label-coverage` | B | stored predictions; realized error vs persistence; labels per week |
| `GET /alerts`, `POST /alerts/{id}/ack`, `GET /alerts/threshold-suggestion` | A | open or all alerts; acknowledge; cost-based threshold |
| `GET /actuator/bench-pump` | A | `{on, reason, since, rule_id}` for the bench relay |
| `POST /admin/jobs/{forecast\|retrain\|promote\|alerts}`, `GET /admin/jobs/{id}` | A (alerts) / B (others) | API-key-protected background jobs |

## 8. ML conventions

**Task specs** (`ml/tasks/<name>.yaml`) define everything about a modeling problem: data source (DB or CRP files), target channel and transform (`log1p` for concentrations), resolution, horizon, features (channels, lags, rolling windows, calendar, external), event threshold, split scheme, baselines, models, and `min_labels_warn`. The runner contains no task-specific logic.

**Planned tasks:**
- `crp_chla_<station>` and `crp_tp_<station>`: real CRP monthly or quarterly data. Predict a sample from same-sample measurements plus lags of previous samples. This demonstrates the method, not a 7-day forecaster. Chlorophyll event threshold: 10 ug/L (the alert level used in the studies we follow).
- `sim_phosphate_nowcast`: soft sensor, phosphate from turbidity, tds, temp, ph (sparse grab-sample labels). Simulated: pipeline validation only.
- `sim_bloom_risk_7d`: probability that chlorophyll_a >= 10 ug/L within 7 days, on daily aggregates with `--dense-labels`. Simulated: pipeline validation only.
- `bench_spike_phosphate`: spec only until bench data exists (known KH2PO4 spikes in buckets).

**Model ladder (always this order):** persistence -> seasonal climatology (monthly tasks) -> ridge on log1p target -> RandomForest -> LightGBM. All behind one interface: `fit`, `predict`, `predict_interval`, `explain`.

**Persistence definition:** the latest known good value (or latest label) at issue time, carried forward. Report what it means for each task.

**Splits:** walk-forward expanding window with a configurable gap, then one final chronological hold-out (last 20% by time), evaluated once per run. Tuning happens only inside walk-forward. A test fails if any train timestamp is >= any test timestamp.

**Features:** lags and rolling windows use `closed='left'` (strictly before issue time). Only `quality='good'` rows. Irregular sampling handled explicitly (previous sample, not previous day, for monthly data).

**Uncertainty:** split-conformal intervals from walk-forward residuals (document it in each report). 90% intervals by default.

**Metrics:** MAE, RMSE, R2, skill vs persistence = `1 - MAE/MAE_persistence`. For threshold events: precision, recall, F1, PR-AUC, confusion matrix, base rate. Bootstrap 90% confidence intervals. Loud warnings when `n_train < 50`, when the hold-out has fewer than 10 positive events, or when `R2 > 0.95` on fewer than 200 samples.

**Leakage defenses:** a deliberately leaky feature must trip the guard (test); a shuffled-target run must fall to baseline-level skill, otherwise the report says `LEAKAGE SUSPECTED`.

**Reports:** `python -m ml.run --task <name>` writes `ml/reports/<task>/<UTC-date>/report.md`, `metrics.json` and figures. The first lines state n_train, n_test, number of positives, what persistence means, and the verdict in plain words ("beats persistence by X% MAE" or "does NOT beat persistence").

**Registry and promotion:** `train()` saves a joblib artifact and a `candidate` row. A candidate is promoted only if, on the most recent held-out window, MAE is at least 5% better than the champion's, event recall is not worse by more than 0.05, and at least 10 evaluation labels exist; otherwise "insufficient evidence, champion kept". Every decision is written to `models.notes`.

**Forecast and scorecard:** `run_forecast` writes predictions for the champion **and** `persistence-v1` at the same issue time, horizon and nodes. The scorecard joins predictions to labels (same node and target, `|valid_at - label ts| <= tolerance`) and reports per model_version: n, MAE, bias, interval coverage, event precision/recall, and skill vs `persistence-v1`.

## 9. Alerts

- Rules in `config/alerts.yaml`; every threshold is a `PLACEHOLDER` with a comment on how to set it. Rules: `bloom_risk` (watch and warning levels on `p_event`), `phosphate_high`, `sensor_silent` (no data > 30 min), `analyzer_untrusted`.
- The engine uses hysteresis, a minimum number of consecutive evaluations before firing, one open alert per rule and node, and auto-resolve. Notifier failures never crash it.
- Notifiers: the `alerts` table (dashboard), a log file, and Telegram only when both token and chat id are set.
- Cost-based threshold: `p* = C_FP / (C_FP + C_FN)` (valid for calibrated probabilities). Example: `C_FP=1, C_FN=9` gives `p*=0.1`. Expose it as `GET /alerts/threshold-suggestion`.
- The action is an **advisory** (dashboard banner, optional Telegram) and a **bench pump demo** via `GET /actuator/bench-pump`. No claim of in-river treatment.

## 10. Dashboard and twin

- `dashboard/` only calls the API. No SQL, no ML code, no business logic in pages.
- Pages: Overview, Live signals, Forecast, Model lab, Scorecard, Reach view, Scenario, Admin (label entry, job buttons).
- Persistent banner whenever displayed rows include `source='simulator'`.
- Every chart has axis titles with units. Every page works with an empty database.
- **Mass balance** (`dashboard/twin/massbalance.py`): `C_down = (Q_up*C_up + Q_eff*C_eff) / (Q_up + Q_eff)`, concentrations in mg/L as P, flows in m3/s. Always labeled "first-order estimate: ignores uptake, settling and mixing time". Test with illustrative numbers (not real river flows): `Q_up=10, C_up=0.05, Q_eff=0.5, C_eff=2.82` gives `0.1819`; with `C_eff=0.1` gives `0.0524`.

## 11. Domain notes (from our own research documents; add nothing else as fact)

- **Sites (two-site design, not yet fixed):** one node near the Laredo water-plant intake (IBWC CRP station 13202), one below the effluent inputs; `config/reach.yaml` holds placeholders. The Zacate Creek WWTF discharges to the river (2020 permit application: 2.82 mg/L total phosphorus, 21.4 mg/L NH3-N). The intakes appear to sit upstream of the main effluent inputs (an inference to verify), so do not hard-code the claim that effluent drives intake bloom risk.
- **Related CRP stations:** 13200 (upstream of the Zacate confluence), 13196, 13189 (Falcon Reservoir). CRP files are Excel, one per station, 1995 to present, monthly or quarterly grab samples (about 12 per station-year). Files must be downloaded by a person into `data/raw/crp/`.
- **Flow:** use IBWC gauge discharge, not the YF-S201. Exports are CSV files the team downloads.
- **Phosphate labels:** Hanna HI713 reads 0 to 2.50 ppm as PO4 (+/-0.04 ppm +/-4%). River samples should fall in range; effluent may need dilution.
- **Analyzer:** vanadomolybdate colorimetry at about 375 nm, one cuvette, four pumps, a cycle roughly every 90 minutes, a reference run every Nth cycle. A cycle is untrusted when the reference drifts, the I0 light level falls, or the cycle fails. Untrusted cycles are never used for training.
- **Sensor interference:** a TDS probe disturbs the pH reading for over 10 minutes after each TDS reading (user reports). Our pH/TDS isolator was cut from the budget, so firmware spaces TDS reads and flags pH `suspect` with `ph_settling`. A turbidity sensor in a review drifted after about 8 hours.
- **Modeling evidence we follow:** at this data size boosted trees and random forests match deep models; persistence is hard to beat at short horizons; the main risk is labels, not model choice. Chlorophyll alert level 10 ug/L.
- **Scope open questions:** whether leak detection (pressure transducers) stays in scope; physical node locations; advisor accuracy target. Treat as unknown.

## 12. Definition of done

**Nov 15 feature freeze, Nov 20 demo.** Done means all of:
1. A reading (real or simulated) lands in the DB and shows on the dashboard with its quality flag.
2. A trained, honestly backtested model exists for at least one real-data task (CRP) and the simulated tasks, each shown next to persistence.
3. Predictions are stored with model version and issue time; a later label produces a realized-error row in the scorecard.
4. A rule turns a model output into an alert (dashboard, optional Telegram) and flips the bench actuator.
5. `make demo` works from a fresh clone using only the README.
6. `docs/CLAIMS.md` states what we can and cannot claim.

**If behind, cut in this order:** Telegram, scenario slider, SHAP plots, reach view polish, bench actuator, second CRP station. **Never cut:** idempotent ingest, simulator labels, baseline comparison, scorecard join, the simulated-data banner.

## 13. Working agreements (apply to every task)

- Plan before code when the prompt says so; otherwise start building and keep the plan to a few lines.
- Build only what the prompt asks. Put extra ideas in a **Suggestions** list at the end instead of building them.
- If something blocks you and the answer is not in this file, ask one short question. Otherwise pick the simplest reasonable option and record it in `docs/ASSUMPTIONS.md`.
- Every change comes with tests. Run `make test` and `make lint` before saying you are done and show the real results. Never claim something works without running it; if you could not run it, say so.
- Foundation build (Phases 0 to 3): one branch `feat/foundation-p0-p3`, commits prefixed `P0:` to `P3:`, a lightweight git tag `p<N>-done` at each phase boundary and `foundation-done` at the end. After the handoff: one branch per phase (`feat/p<N>-<name>`). Always: small commits, clear messages, no force-push, no merging your own branch. Never commit secrets, data files or model artifacts.
- Never change the contract, DB schema or another owner's files without asking. A contract change bumps `schema_version` and is announced to all three developers.
- Finish every task with: files created or changed; commands run with pass or fail; assumptions made; open questions; suggestions. (The foundation build ends with the report structure given in its build prompt instead.)
