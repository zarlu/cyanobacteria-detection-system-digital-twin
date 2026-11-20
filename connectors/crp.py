"""Load the cleaned CRP table into the API through ``POST /ingest``.

``python -m connectors.crp`` reads ``data/clean/crp_long.csv`` (written by
``python -m ml.data.crp clean``) and posts every row whose channel exists in
``contract/channels.yaml``, one message stream per station (``node_id`` as in the file, for
example ``crp-13202``; ``source='CRP'``). Rows on channels outside the contract (the ``crp_*``
lab channels, specific conductance included) are counted and skipped: ``/ingest`` would reject
them. The row's quality goes along unchanged; server QC may only downgrade it. Censoring is kept
in ``meta`` (``censored``, ``detection_limit``). Re-running changes nothing (idempotent ingest).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from contract.registry import load_registry
from sim.client import SimClient

log = logging.getLogger("connectors.crp")

REPO = Path(__file__).resolve().parents[1]
DEFAULT_PATH = REPO / "data" / "clean" / "crp_long.csv"
SOURCE = "CRP"
BATCH = 1000  # readings per /ingest message


def _meta(row: pd.Series) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    if isinstance(row["meta"], str) and row["meta"]:
        meta.update(json.loads(row["meta"]))
    if row["censored"] in ("left", "right"):
        meta["censored"] = row["censored"]
        if pd.notna(row["detection_limit"]):
            meta["detection_limit"] = float(row["detection_limit"])
    if isinstance(row["sample_id"], str) and row["sample_id"]:
        meta["sample_id"] = row["sample_id"]
    if isinstance(row["flag_reason"], str) and row["flag_reason"]:
        meta["reason"] = row["flag_reason"]
    return meta


def build_messages(
    long: pd.DataFrame, batch: int = BATCH, now: datetime | None = None
) -> tuple[list[dict[str, Any]], Counter]:
    """Contract messages for the contract channels of ``long``, plus skipped rows per channel."""
    registry = load_registry()
    in_contract = long["channel"].isin(list(registry))
    skipped = Counter(long.loc[~in_contract, "channel"])
    rows = long[in_contract & (long["source"] == SOURCE)]
    sent_at = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    messages: list[dict[str, Any]] = []
    for node_id, group in rows.groupby("node_id", sort=True):
        readings = [
            {
                "ts": r["ts_utc"],
                "channel": r["channel"],
                "value": float(r["value"]),
                "unit": r["unit"],
                "quality": r["quality"],
                "kind": r["kind"],
                "meta": _meta(r),
            }
            for _, r in group.sort_values(["ts_utc", "channel"]).iterrows()
        ]
        for start in range(0, len(readings), batch):
            messages.append(
                {
                    "schema_version": 1,
                    "node_id": str(node_id),
                    "source": SOURCE,
                    "sent_at": sent_at,
                    "firmware": None,
                    "readings": readings[start : start + batch],
                }
            )
    return messages, skipped


def load(path: Path, client: Any, batch: int = BATCH) -> dict[str, Any]:
    """Post the file through ``client.post('/ingest', message)``; return the totals."""
    long = pd.read_csv(path, dtype={"station": str}, low_memory=False)
    messages, skipped = build_messages(long, batch)
    for message in messages:
        client.post("/ingest", message)
    stats = client.stats
    return {
        "rows_in_file": int(len(long)),
        "rows_outside_contract_skipped": int(sum(skipped.values())),
        "skipped_channels": dict(sorted(skipped.items())),
        "nodes": len({m["node_id"] for m in messages}),
        "requests": stats.requests,
        "accepted": stats.accepted,
        "duplicates": stats.duplicates,
        "rejected": stats.rejected,
        "failed_requests": stats.failed_requests,
        "rejection_reasons": dict(stats.rejection_reasons),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--path", type=Path, default=DEFAULT_PATH)
    p.add_argument("--api-url", default=os.environ.get("API_URL", "http://localhost:8000"))
    p.add_argument("--api-key", default=os.environ.get("API_KEY"), help="default: API_KEY env")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if not args.api_key:
        log.error("no API key: set API_KEY (copy .env.example to .env) or pass --api-key")
        return 2
    if not args.path.is_file():
        log.error("%s not found; run `python -m ml.data.crp clean` first", args.path)
        return 2
    totals = load(args.path, SimClient(args.api_url, args.api_key))
    print(json.dumps(totals, indent=2))
    return 1 if totals["failed_requests"] or totals["rejected"] else 0


if __name__ == "__main__":
    sys.exit(main())
