# ml/

Task specs, features, chronological splits, baselines, models, evaluation, leakage drills and
reports (CLAUDE.md section 8). Phase 4a built `ml/data/` (CRP cleaning); Phase 4b built the
evaluation harness and the CRP tasks. The registry, forecast job, scorecard and
`api/routers/ml.py` are later phases.

## Setup

    make setup-ml            # scikit-learn, LightGBM, SciPy, matplotlib, SHAP, joblib, openpyxl
    brew install libomp      # macOS only: LightGBM needs the OpenMP runtime

CRP files must be downloaded by a person into `data/raw/crp/` (gitignored), then
`python -m ml.data.crp clean` writes `data/clean/crp_long.csv`, which every CRP task reads.

## Run a task

    uv run python -m ml.run --task crp_chla_pooled          # full run
    uv run python -m ml.run --task crp_chla_pooled --fast   # development only, labeled FAST

Output: `ml/reports/<task>/<UTC-date>/report.md`, `metrics.json` and figures (`-FAST` suffix on
the folder for a fast run). Same inputs and seeds give a byte-identical `metrics.json`.

Tasks (`ml/tasks/*.yaml`; `_crp_base.yaml` is the shared base, not runnable):
`crp_chla_pooled` (headline), `crp_chla_13202`, `crp_chla_15816`, `crp_chla_17410`,
`crp_chla_pooled_allyears` (sensitivity), `crp_tp_pooled`, `crp_tp_13202`.

## Modules

| file | what it does |
|---|---|
| `config.py` | load a task YAML, resolve `extends`, validate |
| `source.py` | good rows only, one row per (station, date), censored-label rules |
| `features.py` | config-driven feature sets, fold-local median imputation |
| `splits.py` | walk-forward by calendar year, one final hold-out, chronology assertions |
| `baselines.py` | persistence, seasonal climatology, base rate |
| `models.py` | logistic/ridge, RandomForest, LightGBM: `fit`, `predict`, `predict_interval`, `explain` |
| `eval.py` | metrics, bootstrap over distinct dates, paired differences, verdict sentences |
| `drills.py` | leakage checks; the only place a random split may exist |
| `pipeline.py` | fits every predictor on every fold (shared by the runner and the drills) |
| `run.py`, `report.py`, `figures.py` | the command, report.md, figures |

## Rules the harness enforces

- No random splits. A test greps `ml/` for random splitters; only
  `drills.invalid_random_split_demo` may contain one, and its output is labeled NOT valid.
- Every model result sits next to persistence and seasonal climatology. "beats X" is written
  only when the 90% bootstrap interval of the paired difference excludes zero.
- No hyperparameter search: every setting is fixed in the task YAML.
- Stations, channels, thresholds and model settings live in YAML, never in Python.
- Real (`source=CRP`) and simulated data are never mixed.
