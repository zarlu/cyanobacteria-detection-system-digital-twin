# connectors/ (owner: A) - stub

CSV-export connectors that turn IBWC gauge discharge and weather exports into `/ingest`
messages (`kind='external'`, `source='IBWC'|'weather'`, `meta.original_unit`). Not built in the
foundation. The simulator emulates these sources until the connectors exist.
