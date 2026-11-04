# Data contract v1

Nodes, the analyzer, connectors and the simulator all POST one JSON message to `POST /ingest`
with header `X-API-Key: <API_KEY>`. The response is per reading:
`{"accepted": n, "duplicates": n, "rejected": [{"index": i, "reason": "..."}]}`.
Valid readings are stored even when others in the same batch are rejected.

## Message

| field | type | rules |
|---|---|---|
| `schema_version` | int | must be `1` |
| `node_id` | string | lowercase slug `^[a-z0-9][a-z0-9_-]{0,63}$`; unknown ids auto-create a node with role `unknown` |
| `source` | enum | default source for every reading: `sensor, analyzer, HI713, CRP, IBWC, weather, manual, simulator` |
| `sent_at` | ISO-8601 | aware timestamp (`Z` or offset), normalized to UTC |
| `firmware` | string or null | node firmware version |
| `readings` | array | at least one reading |

## Reading

| field | type | rules |
|---|---|---|
| `ts` | ISO-8601 | aware, normalized to UTC, truncated to whole seconds; naive -> rejected; more than 10 min in the future -> rejected; any age in the past accepted |
| `channel` | string | must exist in `channels.yaml` |
| `value` | number | finite (no NaN, no infinity, no strings, no booleans) |
| `unit` | string | must equal the channel's canonical unit in `channels.yaml` |
| `quality` | enum | `good` (default), `suspect`, `bad`. Server QC may downgrade, never upgrade |
| `kind` | enum | `measurement`, `label`, `external`; defaults to the channel's `default_kind` |
| `source` | enum | overrides the message source for this reading |
| `meta` | object | optional. Conventional keys: `reason` (`out_of_range, spike, stuck, saturated, ph_settling, ref_drift, led_low, sensor_error`), `cycle_id`, `is_reference_run`, `original_unit`, `emulates` (simulator only), `censored`, `sample_id`; the server adds `qc_reason` when QC downgrades a reading that already carried a `reason` |

**Uniqueness key:** `(node_id, channel, ts, kind, source)`. A repeat is a `duplicate`, not an
error. Two readings with the same key inside one message make the message invalid under the strict
`IngestMessage` model; the API rejects the later one with reason `duplicate_in_batch`.

**Units are explicit.** `phosphate_po4` is mg/L **as PO4**; `total_phosphorus` is mg/L **as P**
(1 mg/L PO4 = 0.3261 mg/L P). Both carry the unit string `mg/L`; the channel name carries the basis.

## Worked example

A node posts a temperature and a pH reading that the firmware already flagged as settling after a
TDS read:

```json
{
  "schema_version": 1,
  "node_id": "node-intake",
  "source": "sensor",
  "sent_at": "2026-10-10T15:04:05Z",
  "firmware": "0.1.0",
  "readings": [
    {"ts": "2026-10-10T15:04:00Z", "channel": "water_temp", "value": 27.4,
     "unit": "degC", "quality": "good", "kind": "measurement", "meta": {}},
    {"ts": "2026-10-10T15:04:00Z", "channel": "ph", "value": 7.9,
     "unit": "pH", "quality": "suspect", "meta": {"reason": "ph_settling"}}
  ]
}
```

Response on first send: `{"accepted": 2, "duplicates": 0, "rejected": []}`.
Response when the same message is re-sent: `{"accepted": 0, "duplicates": 2, "rejected": []}`.

If the pH reading had `"ts": "2026-10-10T15:04:00"` (naive) the response would be
`{"accepted": 1, "duplicates": 0, "rejected": [{"index": 1, "reason": "ts is a naive timestamp; ..."}]}`.

The channel table (units, ranges, step limits) lives in `channels.yaml`; CLAUDE.md section 5 mirrors it.
