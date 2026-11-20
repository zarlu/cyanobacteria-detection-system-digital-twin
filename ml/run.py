"""A8. Run one task: `python -m ml.run --task <name> [--fast]`.

Writes `ml/reports/<task>/<UTC-date>/report.md`, `metrics.json` and figures. A `--fast` run
(fewer trees and resamples, development only) goes to `<UTC-date>-FAST/` and is labeled FAST.

The runner has no task-specific logic: everything comes from `ml/tasks/<name>.yaml`.
Same inputs and seeds give an identical metrics.json.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ml import baselines as baselines_mod
from ml.config import REPO, TASK_DIR, list_tasks, load_task, resolve_feature_set
from ml.drills import (
    LEAKAGE_SUSPECTED,
    invalid_random_split_demo,
    shuffled_label_check,
    truncation_check,
)
from ml.eval import NO_POSITIVES, EvalSet, average_precision, evaluate, mae, slice_rows
from ml.features import build_features, complete_case_share
from ml.models import permutation_importance
from ml.pipeline import FoldRun, make_evalset, run_folds, split_key
from ml.source import build_frame, data_path, file_sha256, load_long, make_labels, transform_fn
from ml.splits import SplitPlan, make_splits

REPORT_ROOT = REPO / "ml" / "reports"


def predictor_label(task: dict[str, Any], key: str) -> str:
    """Human name of a predictor, with the deployable flag of its feature set."""
    algo, set_name = split_key(key)
    if set_name is None:
        return algo.replace("_", " ")
    _, deployable = resolve_feature_set(task, set_name)
    flag = "deployable" if deployable else "diagnostic, not deployable"
    return f"{algo} on {set_name} ({flag})"


def _evaluate_block(es: EvalSet, task: dict[str, Any], kind: str, pooled: bool) -> dict[str, Any]:
    cfg = task["eval"]
    base_names = list(task["baselines"][kind])

    def label(key: str) -> str:
        return predictor_label(task, key)

    block = {"all": evaluate(es, cfg, base_names, label=label)}
    if kind == "regression":
        numeric = np.flatnonzero(~es.censored)
        block["numeric_only"] = evaluate(es, cfg, base_names, rows=numeric, label=label)
    by_station = pooled and bool(cfg.get("slice_by_station", True))
    block["slices"] = {
        name: evaluate(es, cfg, base_names, rows=rows, with_differences=False, label=label)
        for name, rows in slice_rows(es, cfg.get("slices", {}), by_station).items()
    }
    return block


def _fold_table(plan: SplitPlan, labels: pd.DataFrame, kind: str) -> list[dict[str, Any]]:
    return [f.describe(labels, kind) for f in plan.folds]


def _conformal(
    task: dict[str, Any],
    es_wf: EvalSet,
    es_hold: EvalSet | None,
    hold_run: FoldRun | None,
    X: dict[str, pd.DataFrame],
) -> dict[str, Any]:
    """Split-conformal intervals: half-width from the walk-forward residuals (transformed
    scale), applied to the hold-out predictions, coverage counted in the original units."""
    cfg = task.get("intervals", {})
    level = float(cfg.get("level", 0.90))
    min_resid = int(cfg.get("min_residuals", 30))
    _, inverse = transform_fn(task["target"].get("regression", {}).get("transform"))
    out: dict[str, Any] = {
        "method": "split-conformal on absolute walk-forward residuals (transformed scale)",
        "level": level,
        "n_residuals": int(len(es_wf)),
        "min_residuals": min_resid,
        "too_few_residuals": bool(len(es_wf) < min_resid),
        "predictors": {},
    }
    for key in es_wf.preds:
        if split_key(key)[1] is None:
            continue
        residuals = es_wf.y - es_wf.preds[key]
        entry: dict[str, Any] = {}
        if es_hold is not None and hold_run is not None and key in hold_run.fitted:
            model, test_idx = hold_run.fitted[key][0]
            model.calibrate(residuals, level)
            lo_t, hi_t = model.predict_interval(X[split_key(key)[1]].iloc[test_idx])
            entry["half_width_transformed"] = model.q_
            if math.isfinite(model.q_):
                lo, hi = inverse(lo_t), inverse(hi_t)
                inside = (es_hold.value >= lo) & (es_hold.value <= hi)
                entry["holdout_coverage"] = float(inside.mean())
                entry["holdout_median_width"] = float(np.median(hi - lo))
            else:
                entry["holdout_coverage"] = None
                entry["holdout_median_width"] = None
        out["predictors"][key] = entry
    return out


def _importance(
    task: dict[str, Any],
    kind: str,
    labels: pd.DataFrame,
    X: dict[str, pd.DataFrame],
    wf_run: FoldRun,
) -> dict[str, Any]:
    cfg = task.get("explain", {})
    y = labels["y"].to_numpy(dtype=float)

    def score(y_true: np.ndarray, pred: np.ndarray) -> float:
        return average_precision(y_true, pred) if kind == "event" else -mae(y_true, pred)

    out = {}
    for key, fitted in wf_run.fitted.items():
        set_name = split_key(key)[1]
        triples = [(model, X[set_name].iloc[te], y[te]) for model, te in fitted]
        out[key] = permutation_importance(
            triples, score, int(cfg.get("n_repeats", 5)), int(cfg.get("seed", 0))
        )
    return {
        "method": (
            "permutation importance on the walk-forward test rows: mean drop in "
            + ("PR-AUC" if kind == "event" else "negative MAE (transformed scale)")
            + " when one feature is shuffled within each fold's test rows"
        ),
        "predictors": out,
    }


def _station_year_counts(labels: pd.DataFrame, rows: np.ndarray) -> dict[str, int]:
    sub = labels.iloc[rows]
    counts = sub.groupby([sub["station"], sub["date"].dt.year]).size()
    return {f"{station}/{year}": int(n) for (station, year), n in counts.items()}


def _warnings(task: dict[str, Any], kind: str, res: dict[str, Any]) -> list[str]:
    w = task.get("warnings", {})
    out: list[str] = []
    tag = f"[{kind}]"
    folds = res["folds"]
    if not folds:
        out.append(f"{tag} NO walk-forward fold could be scored: every fold was skipped.")
    for s in res["skipped_folds"]:
        out.append(f"{tag} fold {s['fold']} skipped: {s['reason']}.")
    if res.get("holdout_skipped"):
        out.append(f"{tag} final hold-out NOT evaluated: {res['holdout_skipped']}.")
    min_n = int(w.get("min_n_train", 50))
    small = [f"{f['fold']} ({f['n_train']})" for f in folds if f["n_train"] < min_n]
    hold = res.get("holdout_fold")
    if hold and hold["n_train"] < min_n:
        small.append(f"holdout ({hold['n_train']})")
    if small:
        out.append(f"{tag} n_train below {min_n} in: {', '.join(small)}.")
    if kind == "event":
        min_pos = int(w.get("min_holdout_positives", 10))
        if hold and hold["test_positives"] < min_pos:
            out.append(
                f"{tag} the hold-out has {hold['test_positives']} positives, fewer than "
                f"{min_pos}: hold-out event metrics are very uncertain."
            )
        zero = [f["fold"] for f in folds if f["test_positives"] == 0]
        if zero:
            out.append(f"{tag} walk-forward folds with zero positives: {', '.join(zero)}.")
        for scope in ("walk_forward", "holdout"):
            block = res.get(scope)
            if not block:
                continue
            zero_slices = [n for n, s in block["slices"].items() if s.get("positives") == 0]
            if zero_slices:
                out.append(
                    f"{tag} {scope.replace('_', '-')} slices with zero positives "
                    f"({NO_POSITIVES}): {', '.join(zero_slices)}."
                )
    else:
        r2_max, n_max = float(w.get("r2_suspicious", 0.95)), int(w.get("r2_suspicious_max_n", 200))
        for scope in ("walk_forward", "holdout"):
            block = res.get(scope)
            if not block or block["all"]["n"] >= n_max:
                continue
            for key, entry in block["all"]["predictors"].items():
                r2_value = entry["metrics"]["r2"]["value"]
                if r2_value is not None and np.isfinite(r2_value) and r2_value > r2_max:
                    out.append(
                        f"{tag} R2 of {key} is {r2_value:.3f} on {block['all']['n']} samples "
                        f"({scope}): above {r2_max} on fewer than {n_max} samples, suspicious."
                    )
        conf = res.get("conformal")
        if conf and conf["too_few_residuals"]:
            out.append(
                f"{tag} only {conf['n_residuals']} walk-forward residuals (fewer than "
                f"{conf['min_residuals']}): the conformal intervals are not meaningful."
            )
    share_min = float(w.get("min_recent_complete_share", 0.5))
    for set_name, av in res["feature_availability"].items():
        share = av["recent_complete_share"]
        if share is not None and share < share_min:
            years = " and ".join(str(y) for y in av["recent_years"])
            out.append(
                f"{tag} feature set {set_name}: only {share:.0%} of the {av['recent_n']} labels "
                f"in {years} have every feature (below {share_min:.0%}); the rest are imputed."
            )
    per_min = int(w.get("min_labels_per_station_year", 4))
    counts = res["test_station_year_counts"]
    low = {k: v for k, v in counts.items() if v < per_min}
    if low:
        out.append(
            f"{tag} {len(low)} of {len(counts)} station-years in the test windows have fewer "
            f"than {per_min} labels (minimum {min(low.values())})."
        )
    drills = res["leakage_checks"]
    for t in drills["truncation"]:
        if not t["passed"]:
            out.append(
                f"{tag} {LEAKAGE_SUSPECTED}: feature set {t['feature_set']} changes when later "
                f"rows are removed ({t['dates_with_mismatch']} dates)."
            )
    shuffled = drills["shuffled_labels"]
    if shuffled.get("suspected"):
        names = [k for k, p in shuffled["predictors"].items() if p["suspected"]]
        out.append(f"{tag} {LEAKAGE_SUSPECTED}: shuffled-label skill for {', '.join(names)}.")
    return out


def run_kind(task: dict[str, Any], frame: pd.DataFrame, kind: str) -> dict[str, Any]:
    """Everything for one target kind (event or regression)."""
    labels, label_info = make_labels(frame, task, kind)
    plan = make_splits(labels, task["split"], kind)
    sets = list(task["use_feature_sets"])
    pooled = len(task["data"]["stations"]) > 1
    X = {s: build_features(frame, task, s).loc[labels["row"]].reset_index(drop=True) for s in sets}
    recent_years = int(task.get("warnings", {}).get("recent_years", 2))

    res: dict[str, Any] = {
        "kind": kind,
        "labels": label_info,
        "folds": _fold_table(plan, labels, kind),
        "skipped_folds": plan.skipped,
        "holdout_start": str(plan.holdout_start.date()) if plan.holdout_start is not None else None,
        "holdout_dates": plan.n_holdout_dates,
        "holdout_skipped": plan.holdout_skipped,
        "holdout_fold": plan.holdout.describe(labels, kind) if plan.holdout else None,
        "baseline_meaning": {
            name: baselines_mod.meaning(name, kind, task) for name in task["baselines"][kind]
        },
        "feature_availability": {},
        "predictor_labels": {},
    }
    for s in sets:
        specs, deployable = resolve_feature_set(task, s)
        res["feature_availability"][s] = {
            "deployable": deployable,
            "columns": [str(c) for c in X[s].columns],
            **complete_case_share(X[s], labels["date"], recent_years),
        }

    wf_run = run_folds(labels, X, plan.folds, task, kind, keep_models=True)
    es_wf = make_evalset(labels, wf_run, task, kind) if plan.folds else None
    res["walk_forward"] = _evaluate_block(es_wf, task, kind, pooled) if es_wf is not None else None

    hold_run = es_hold = None
    if plan.holdout is not None:
        hold_run = run_folds(labels, X, [plan.holdout], task, kind, keep_models=True)
        es_hold = make_evalset(labels, hold_run, task, kind)
    res["holdout"] = _evaluate_block(es_hold, task, kind, pooled) if es_hold is not None else None

    keys = list((es_wf or es_hold).preds) if (es_wf or es_hold) is not None else []
    res["predictor_labels"] = {k: predictor_label(task, k) for k in keys}

    if kind == "regression" and es_wf is not None:
        res["conformal"] = _conformal(task, es_wf, es_hold, hold_run, X)
    res["importance"] = _importance(task, kind, labels, X, wf_run) if plan.folds else None

    test_rows = np.concatenate(
        [wf_run.rows, hold_run.rows if hold_run is not None else np.array([], dtype=int)]
    ).astype(int)
    res["test_station_year_counts"] = _station_year_counts(labels, test_rows)

    pre_holdout = (
        np.flatnonzero((labels["date"] < plan.holdout_start).to_numpy())
        if plan.holdout_start is not None
        else np.arange(len(labels))
    )
    res["leakage_checks"] = {
        "truncation": [truncation_check(frame, task, s) for s in sets],
        "shuffled_labels": shuffled_label_check(labels, X, plan.folds, task, kind),
        "leaky_feature_guard": (
            "assert_no_future_dependence (the truncation guard) raises on a feature built from "
            "the next sample's target; asserted by "
            "tests/ml/test_drills.py::test_leaky_feature_trips_the_guard"
        ),
        "random_split_demo": invalid_random_split_demo(labels, X, pre_holdout, task, kind),
    }
    res["month_profile"] = _month_profile(task, kind, labels, es_wf)
    res["warnings"] = _warnings(task, kind, res)
    return res


def _month_profile(
    task: dict[str, Any], kind: str, labels: pd.DataFrame, es: EvalSet | None
) -> dict[str, Any] | None:
    """Per calendar month: labels and positives (all labels), and the mean out-of-sample
    prediction of the baselines and headline models (walk-forward rows). Used by a figure."""
    months = labels["date"].dt.month.to_numpy()
    out: dict[str, Any] = {"labels": {}, "walk_forward": {}}
    for m in range(1, 13):
        sel = months == m
        entry = {"n": int(sel.sum())}
        if kind == "event":
            entry["positives"] = int(labels["y"].to_numpy()[sel].sum())
        out["labels"][str(m)] = entry
    if es is None:
        return out
    es_months = np.asarray(es.dates, dtype="datetime64[M]").astype(int) % 12 + 1
    headline = task["headline_feature_set"]
    keys = [k for k in es.preds if split_key(k)[1] in (None, headline)]
    source = es.preds if kind == "event" else es.preds_value
    truth = es.y if kind == "event" else es.value
    for m in range(1, 13):
        sel = es_months == m
        entry = {"n": int(sel.sum())}
        if sel.any():
            entry["observed_mean"] = float(truth[sel].mean())
            entry["predicted_mean"] = {k: float(source[k][sel].mean()) for k in keys}
        out["walk_forward"][str(m)] = entry
    return out


def reference_comparison(task: dict[str, Any], targets: dict[str, Any]) -> dict[str, Any] | None:
    ref = task.get("reference_probe")
    if not ref:
        return None
    tol = float(ref["tolerance"])
    rows = []
    for kind in ("event", "regression"):
        spec = ref.get(kind)
        block = (targets.get(kind) or {}).get("walk_forward")
        if not spec or not block:
            continue
        for key, ref_value in spec["values"].items():
            entry = block["all"]["predictors"].get(key)
            ours = entry["metrics"][spec["metric"]]["value"] if entry else None
            diff = None if ours is None else ours - ref_value
            checked = key in ref.get("must_check", []) and kind == "event"
            rows.append(
                {
                    "target": kind,
                    "metric": spec["metric"],
                    "predictor": key,
                    "reference": ref_value,
                    "this_run_walk_forward": ours,
                    "difference": diff,
                    "must_be_within_tolerance": checked,
                    "within_tolerance": None if diff is None else bool(abs(diff) <= tol),
                }
            )
    return {"note": ref["note"], "tolerance": tol, "rows": rows}


def merge_warnings(per_kind: list[list[str]]) -> list[str]:
    """One list for the top of the report. A warning with the same text for several target kinds
    is printed once with every kind in its tag, for example `[event, regression]`."""
    tags: dict[str, list[str]] = {}
    for warnings in per_kind:
        for w in warnings:
            tag, _, body = w.partition("] ")
            tags.setdefault(body, []).append(tag.lstrip("["))
    return [f"[{', '.join(kinds)}] {body}" for body, kinds in tags.items()]


def _clean(obj: Any) -> Any:
    """JSON-safe copy: numpy types to Python, non-finite floats to None, floats rounded."""
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return [_clean(v) for v in obj.tolist()]
    if isinstance(obj, (bool, np.bool_)):
        return bool(obj)
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        value = float(obj)
        return round(value, 10) if math.isfinite(value) else None
    return obj


def run_task(task: dict[str, Any]) -> dict[str, Any]:
    """Compute every result of a prepared task (see ml.config.prepare_task). No files written."""
    path = data_path(task)
    frame, frame_info = build_frame(load_long(path), task)
    targets = {kind: run_kind(task, frame, kind) for kind in task["targets"]}
    try:
        shown_path = str(path.relative_to(REPO))
    except ValueError:
        shown_path = path.name
    result = {
        "task": task["name"],
        "description": task.get("description", ""),
        "fast": bool(task.get("fast_mode")),
        "input": {"path": shown_path, "sha256": file_sha256(path)},
        "source": task["data"]["source_filter"],
        "start": task["start"],
        "frame": frame_info,
        "primary_target": task["targets"][0],
        "target": task["target"],
        "headline_feature_set": task["headline_feature_set"],
        "split": task["split"],
        "eval": task["eval"],
        "models": task["models"],
        "seed": task["seed"],
        "notes": task.get("notes", []),
        "targets": targets,
    }
    result["warnings"] = merge_warnings([targets[kind]["warnings"] for kind in task["targets"]])
    result["reference_comparison"] = reference_comparison(task, targets)
    return _clean(result)


def write_outputs(result: dict[str, Any], out_dir: Path, figures: bool = True) -> list[Path]:
    from ml.report import render_report

    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    metrics_path = out_dir / "metrics.json"
    metrics_path.write_text(
        json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )
    written.append(metrics_path)
    figure_names: list[str] = []
    if figures:
        from ml.figures import write_figures

        figure_paths = write_figures(result, out_dir)
        figure_names = [p.name for p in figure_paths]
        written.extend(figure_paths)
    report_path = out_dir / "report.md"
    report_path.write_text(render_report(result, figure_names), encoding="utf-8")
    written.append(report_path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ml.run", description=__doc__)
    parser.add_argument("--task", required=True, help=f"one of: {', '.join(list_tasks())}")
    parser.add_argument("--fast", action="store_true", help="development only; labeled FAST")
    parser.add_argument("--task-dir", type=Path, default=TASK_DIR)
    parser.add_argument("--out-root", type=Path, default=REPORT_ROOT)
    parser.add_argument("--no-figures", action="store_true")
    args = parser.parse_args(argv)

    task = load_task(args.task, args.task_dir, fast=args.fast)
    result = run_task(task)
    day = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d") + ("-FAST" if args.fast else "")
    out_dir = args.out_root / task["name"] / day
    written = write_outputs(result, out_dir, figures=not args.no_figures)
    report = (out_dir / "report.md").read_text(encoding="utf-8")
    print(report.split("\n## Metrics", 1)[0].rstrip())
    print("\nwrote:")
    for path in written:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
