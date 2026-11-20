# connectors/

- `python -m connectors.crp`: loads `data/clean/crp_long.csv` (written by
  `python -m ml.data.crp clean`) into the API through `POST /ingest`, contract channels only,
  `source='CRP'`, one node per station. Idempotent. Used by `make demo-real`.
- Not built: IBWC gauge discharge and weather CSV exports (`kind='external'`,
  `source='IBWC'|'weather'`, `meta.original_unit`). The simulator emulates these sources.
