"""``python -m sim.run``: generate a scenario and post it to the API (or write JSONL in dry-run).

Examples::

    python -m sim.run --scenario summer_bloom --days 90 --seed 1 --dense-labels
    python -m sim.run --scenario sensor_faults --days 14 --dry-run --start-date 2026-09-01
    python -m sim.run --scenario baseline --days 1 --mode live --speedup 60
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from db.seed import load_reach
from sim.build import RunOutput, build_run
from sim.client import DryRunWriter, SimClient
from sim.config import load_scenario, scenario_names
from sim.physics import TimeGrid, generate_world

log = logging.getLogger("sim")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = REPO_ROOT / "sim_out"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Command-line options."""
    p = argparse.ArgumentParser(prog="python -m sim.run", description=__doc__.split("\n\n")[0])
    p.add_argument("--scenario", default="baseline", choices=scenario_names())
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--step-min", type=int, default=15)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument(
        "--start-date", type=str, default=None, help="YYYY-MM-DD (UTC); default: N days before now"
    )
    p.add_argument("--mode", choices=["backfill", "live"], default="backfill")
    p.add_argument(
        "--speedup", type=float, default=1.0, help="live mode: simulated seconds per wall second"
    )
    p.add_argument("--api-url", default=os.environ.get("API_URL", "http://localhost:8000"))
    p.add_argument("--api-key", default=os.environ.get("API_KEY"), help="default: API_KEY env")
    p.add_argument(
        "--dry-run", action="store_true", help="write JSONL to --out-dir instead of posting"
    )
    p.add_argument(
        "--dense-labels", action="store_true", help="add a weekly chlorophyll_a label series"
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help="where truth.csv, faults.csv and dry-run JSONL go",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def resolve_start(start_date: str | None, days: int, step_min: int) -> datetime:
    """Start of the run: the given UTC date, or N days before now rounded down to the step."""
    if start_date:
        return datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=UTC)
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    now -= timedelta(minutes=now.minute % step_min)
    return now - timedelta(days=days)


def generate(args: argparse.Namespace) -> RunOutput:
    """Generate the world and all batches for the parsed options (no I/O)."""
    params = load_scenario(args.scenario)
    nodes = load_reach()["nodes"]
    params["_nodes"] = nodes
    grid = TimeGrid(
        resolve_start(args.start_date, args.days, args.step_min), args.days, args.step_min
    )
    world = generate_world(params, grid, args.seed, nodes)
    batch_hours = 24.0 if args.mode == "backfill" else args.step_min / 60.0
    return build_run(world, args.seed, dense_labels=args.dense_labels, batch_hours=batch_hours)


def write_side_files(out: RunOutput, out_dir: Path) -> tuple[Path, Path]:
    """Write ``truth.csv`` (never sent to the API) and ``faults.csv``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    truth_path = out_dir / "truth.csv"
    out.truth.to_csv(truth_path, index=False, float_format="%.5f")
    faults_path = out_dir / "faults.csv"
    with faults_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["type", "node_id", "channel", "start", "end"])
        w.writeheader()
        for f in out.faults:
            w.writerow(f.as_row())
    return truth_path, faults_path


def fault_summary(out: RunOutput) -> list[tuple[str, int, float]]:
    """(type, episodes, total hours) per fault type."""
    count: Counter = Counter()
    hours: Counter = Counter()
    for f in out.faults:
        count[f.type] += 1
        hours[f.type] += (f.end - f.start).total_seconds() / 3600.0
    return [(t, count[t], round(hours[t], 1)) for t in sorted(count)]


def post_all(
    out: RunOutput, client: Any, *, mode: str, speedup: float, sim_start: datetime
) -> None:
    """Post every batch in ``sent_at`` order; live mode paces wall-clock by ``speedup``."""
    wall0 = time.monotonic()
    for i, batch in enumerate(out.batches, start=1):
        if mode == "live":
            due = wall0 + (batch.sent_at - sim_start).total_seconds() / speedup
            delay = due - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        client.post(batch.endpoint, batch.body)
        if i % 50 == 0 or i == len(out.batches):
            log.info(
                "posted %d/%d batches (%d readings accepted so far)",
                i,
                len(out.batches),
                client.stats.accepted,
            )


def print_summary(
    args: argparse.Namespace, out: RunOutput, stats: Any, truth_path: Path, faults_path: Path
) -> None:
    """Human-readable end-of-run summary (CLI output, so print is fine here)."""
    print(
        f"\nscenario={args.scenario} days={args.days} step={args.step_min}min seed={args.seed} "
        f"mode={args.mode}"
        f"{' DRY-RUN' if args.dry_run else ''}"
    )
    print(
        "SIMULATED DATA: the physics are assumptions of ours, not Rio Grande facts "
        "(pipeline validation only)."
    )
    print(
        f"batches={len(out.batches)} requests={stats.requests} accepted={stats.accepted} "
        f"duplicates={stats.duplicates} rejected={stats.rejected} "
        f"failed_requests={stats.failed_requests}"
    )
    if stats.rejection_reasons:
        print("rejection reasons (sample):")
        for reason, n in stats.rejection_reasons.most_common(5):
            print(f"  {n:5d}  {reason}")
    print("\nreadings generated per node and channel:")
    by_node: dict[str, list[tuple[str, int]]] = {}
    for (node, channel), n in sorted(out.readings_per_node_channel.items()):
        by_node.setdefault(node, []).append((channel, n))
    for node, items in by_node.items():
        print(f"  {node}: " + ", ".join(f"{c}={n}" for c, n in items))
    print("\nfaults injected (type, episodes, total hours):")
    rows = fault_summary(out)
    if not rows:
        print("  none")
    for t, n, h in rows:
        print(f"  {t:20s} {n:6d} {h:10.1f}")
    print(f"\ntruth: {truth_path}\nfaults: {faults_path}")


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    out = generate(args)
    truth_path, faults_path = write_side_files(out, args.out_dir)
    sim_start = min(b.sent_at for b in out.batches) if out.batches else datetime.now(UTC)
    if not args.dry_run and not args.api_key:
        log.error("no API key: set API_KEY (copy .env.example to .env) or pass --api-key")
        return 2
    if args.dry_run:
        writer = DryRunWriter(args.out_dir / f"{args.scenario}_seed{args.seed}.jsonl")
        post_all(out, writer, mode="backfill", speedup=1.0, sim_start=sim_start)
        writer.close()
        print(f"dry-run JSONL: {writer.path}")
        stats = writer.stats
    else:
        try:
            httpx.Client(timeout=10.0).get(f"{args.api_url.rstrip('/')}/health").raise_for_status()
        except httpx.HTTPError as exc:
            log.error("API not reachable at %s (%s). Start it with `make api`.", args.api_url, exc)
            return 2
        client = SimClient(args.api_url, args.api_key)
        post_all(out, client, mode=args.mode, speedup=args.speedup, sim_start=sim_start)
        stats = client.stats
    print_summary(args, out, stats, truth_path, faults_path)
    return 1 if stats.failed_requests else 0


if __name__ == "__main__":
    sys.exit(main())
