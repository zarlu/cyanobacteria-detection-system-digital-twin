# Cyanobacteria Detection System Digital Twin

Software for a TAMIU senior-design project: low-cost ESP32 water-quality nodes on the Rio Grande
near Laredo send JSON to an API; a database stores every reading with its quality flag; ML models
(later phases) forecast phosphorus and cyanobacteria-bloom risk; a Streamlit "digital twin"
dashboard shows data, forecasts and alerts.

**The sensors have not arrived.** A simulator that speaks the exact node contract stands in for the
hardware. Everything it produces is labeled `source='simulator'` and is **pipeline validation only,
not river performance**. The simulator's physics are the team's assumptions, not Rio Grande facts.

The rulebook for every Claude Code session on this repository is [CLAUDE.md](CLAUDE.md). New
teammates: start with [docs/HANDOFF.md](docs/HANDOFF.md) (repo map, how to add things, measured
limitations, suggested split of the next phases) and [docs/ASSUMPTIONS.md](docs/ASSUMPTIONS.md).

## Status

| Phase | Content | State |
|---|---|---|
| 0 | scaffold, data contract, database, `/health` | built |
| 1 | ingest and read API | built |
| 2 | sensor simulator | built |
| 3 | dashboard v0 (Overview, Live signals), `make demo` | built |
| 4+ | ML, alerts, firmware, connectors | stubs only (see each folder's README and docs/HANDOFF.md) |

## Quickstart (one command)

```sh
make setup && make demo     # fresh demo database, API on :8000, 60-day simulated backfill, dashboard on :8501
make stop                   # stops both (safe to run when nothing is running)
```

`make demo` uses its own database (`data/demo.db`), starts the API in the background (PID in
`.run/api.pid`, log in `.run/api.log`), waits for `/health`, backfills the `summer_bloom` scenario
for 60 days (seed 1, dense labels), starts Streamlit (PID in `.run/dash.pid`) and prints both URLs.
Everything you see is simulated and labeled as such.

## Step by step

Requirements: Python 3.12 or newer (3.13 used for development), [uv](https://docs.astral.sh/uv/),
`make`, `curl`. Windows users: run everything under WSL.

```sh
make setup        # .venv with pinned deps; creates .env from .env.example if missing (API_KEY is required)
make test         # unit tests (fast; excludes e2e)
make lint         # ruff check + format check
make api          # http://localhost:8000/docs
```

In another shell: `curl -s http://localhost:8000/health`, then fill the database with simulated data:

```sh
make sim ARGS="--scenario summer_bloom --days 90 --seed 1 --dense-labels"
```

The simulator speaks the node contract over HTTP; every row it creates is `source='simulator'`
and the physics are our assumptions (see [sim/README.md](sim/README.md)). Scenarios: `baseline`,
`summer_bloom`, `effluent_step`, `storm_week`, `sensor_faults`. Add `--dry-run` to write JSONL to
`sim_out/` instead of posting.

All `make` targets: `make help`.

## Dashboard

`make dash` starts the Streamlit app (`dashboard/app.py`) against `API_URL`. Pages: **Overview**
(a card per node with last seen, minutes silent, last-hour quality counts and latest key values;
placeholders for alerts and the champion model) and **Live signals** (Plotly time series per node
and channel, quality encoding: good = line, suspect = hollow orange markers, bad = red x; labels as
diamonds; external channels on a second axis; raw or 15 min / 1 h / 1 D resampling via the API;
table with CSV download). Sidebar: API status, display time zone (America/Chicago or UTC, data
stays UTC), time range, 15-second auto-refresh toggle. A banner "SIMULATED DATA - pipeline
validation only" shows whenever displayed rows have `source='simulator'`. The dashboard only
talks to the API (`dashboard/api_client.py`); every page works on an empty database and when the
API is down.

## API quick reference

All timestamps are UTC ISO-8601. Writes need `X-API-Key` (value of `API_KEY`); reads are open.
Interactive docs: `http://localhost:8000/docs`.

```sh
# health: version, db_ok, utc_now
curl -s localhost:8000/health

# ingest one contract-v1 message (see contract/CONTRACT.md); response: accepted/duplicates/rejected
curl -s -X POST localhost:8000/ingest -H "X-API-Key: dev-key-change-me" \
  -H "Content-Type: application/json" -d @contract/examples/valid_sensor_batch.json

# nodes with last_seen, minutes_since_seen and good/suspect/bad counts for the last hour
curl -s localhost:8000/nodes

# channel registry (units, QC limits)
curl -s localhost:8000/channels

# reach configuration (nodes in river order; placeholders)
curl -s localhost:8000/reach

# observations: raw rows (columnar JSON with meta) ...
curl -s "localhost:8000/observations?node_id=node-intake&channel=water_temp&start=2026-10-01T00:00:00Z&end=2026-10-02T00:00:00Z"
# ... or resampled (15min | 1h | 1D) with agg (mean | median | last | max | min); quality = worst in bucket, n = rows
curl -s "localhost:8000/observations?channel=water_temp&resample=1h&agg=mean&min_quality=suspect"

# one label (defaults: channel phosphate_po4, source HI713, unit from the registry)
curl -s -X POST localhost:8000/labels -H "X-API-Key: dev-key-change-me" -H "Content-Type: application/json" \
  -d '{"ts":"2026-10-01T12:00:00Z","node_id":"node-intake","value":0.21,"sample_id":"S1"}'

# bulk labels from CSV (header: ts_utc,node_id,channel,value,unit,source,sample_id,notes); errors per line
curl -s -X POST localhost:8000/labels/csv -H "X-API-Key: dev-key-change-me" -F file=@labels.csv
```

Queries that would touch more than 200,000 rows return 413; narrow the range or filter by node
and channel. Server QC (ranges, spikes, stuck values, TDS saturation) can only downgrade quality.

## Layout

See CLAUDE.md section 4. Each top-level folder has a README saying what lives there and who owns it.
Folders `ml/`, `alerts/`, `connectors/`, `firmware/` are stubs until their phases start.

## Environment

`make setup` copies `.env.example` to `.env` when missing. `API_KEY` is required by the API and the
simulator (the example value is for local development only; change it before exposing the API),
`DB_URL` (default `sqlite:///data/twin.db`), `API_URL`, `TZ_DISPLAY` (dashboard default zone).
`.env`, `data/`, `artifacts/`, `.run/` and `sim_out/` are never committed.
