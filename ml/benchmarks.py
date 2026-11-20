"""Read a task's latest committed report (`metrics.json`) as flat benchmark rows.

Nothing is recomputed here: every number is copied from the file the harness wrote
(`python -m ml.run --task <name>`). FAST development reports are never served.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ml.config import REPO

REPORTS = REPO / "ml" / "reports"
HEADLINE = {"event": "pr_auc", "regression": "mae"}
HIGHER_IS_BETTER = {"pr_auc": True, "mae": False}
BLOCKS = ("walk_forward", "holdout")


def report_tasks(reports: Path = REPORTS) -> list[str]:
    if not reports.is_dir():
        return []
    return sorted(p.name for p in reports.iterdir() if latest_report(p.name, reports) is not None)


def latest_report(task: str, reports: Path = REPORTS) -> Path | None:
    """Newest non-FAST report directory of `task` that holds a metrics.json."""
    base = reports / task
    if not base.is_dir():
        return None
    dirs = sorted(
        d
        for d in base.iterdir()
        if d.is_dir() and "FAST" not in d.name and (d / "metrics.json").is_file()
    )
    return dirs[-1] if dirs else None


def load_metrics(task: str, reports: Path = REPORTS) -> tuple[dict[str, Any], Path] | None:
    path = latest_report(task, reports)
    if path is None:
        return None
    return json.loads((path / "metrics.json").read_text(encoding="utf-8")), path


def _block(block: dict[str, Any], metric: str, labels: dict[str, str]) -> dict[str, Any]:
    everything = block["all"]
    verdicts: dict[str, dict[str, Any]] = {}
    for diff in everything.get("differences", []):
        verdicts.setdefault(diff["model"], {})[diff["baseline"]] = {
            "verdict": diff["verdict"],
            "sentence": diff["sentence"],
        }
    rows = []
    for key, entry in everything["predictors"].items():
        m = entry["metrics"].get(metric, {})
        algo, _, feature_set = key.partition("@")
        rows.append(
            {
                "key": key,
                "label": labels.get(key, key),
                "is_baseline": "@" not in key,
                "algorithm": algo,
                "feature_set": feature_set or None,
                "value": m.get("value"),
                "lo": m.get("lo"),
                "hi": m.get("hi"),
                "other_metrics": {
                    k: v.get("value") for k, v in entry["metrics"].items() if k != metric
                },
                "verdicts": verdicts.get(key, {}),
            }
        )
    return {
        "n": everything.get("n"),
        "n_dates": everything.get("n_dates"),
        "positives": everything.get("positives"),
        "base_rate": everything.get("base_rate"),
        "rows": rows,
    }


def benchmarks(task: str, reports: Path = REPORTS) -> dict[str, Any] | None:
    """Benchmark rows of the task's latest report; None when the task has no report."""
    loaded = load_metrics(task, reports)
    if loaded is None:
        return None
    metrics, path = loaded
    out: dict[str, Any] = {
        "task": metrics["task"],
        "report_date": path.name,
        "report_path": str(path.relative_to(reports.parent.parent))
        if path.is_relative_to(reports.parent.parent)
        else str(path),
        "description": metrics.get("description"),
        "source": metrics.get("source"),
        "primary_target": metrics.get("primary_target"),
        "headline_feature_set": metrics.get("headline_feature_set"),
        "target": metrics.get("target"),
        "ci": metrics.get("eval", {}).get("ci"),
        "warnings": metrics.get("warnings", []),
        "kinds": {},
    }
    for kind, res in metrics["targets"].items():
        metric = HEADLINE[kind]
        entry: dict[str, Any] = {
            "metric": metric,
            "higher_is_better": HIGHER_IS_BETTER[metric],
            "holdout_start": res.get("holdout_start"),
            "baseline_meaning": res.get("baseline_meaning", {}),
            "blocks": {},
        }
        for name in BLOCKS:
            if res.get(name):
                entry["blocks"][name] = _block(res[name], metric, res.get("predictor_labels", {}))
        out["kinds"][kind] = entry
    return out
