"""A6. Metrics, date-level bootstrap intervals, paired differences and verdict sentences.

Bootstrap: distinct sample dates are resampled with replacement (same-day station replicates
stay together), with a fixed seed. The same resamples are used for every predictor, so the
difference between two predictors is a paired difference. Intervals are plain percentile
intervals. A metric that is undefined on a resample (for example PR-AUC with no positives) is
left out of that metric's percentiles.

Verdict rule: "beats X" only when the interval of the paired difference excludes zero in the
favourable direction; "is worse than X" when it excludes zero in the unfavourable direction;
otherwise "shows no demonstrable difference from X".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy.stats import rankdata

NO_POSITIVES = "undefined: no positives"


# ----------------------------------------------------------------------------- point metrics
def average_precision(y: np.ndarray, score: np.ndarray) -> float:
    """PR-AUC as average precision: sum over distinct score thresholds of
    (recall step) * precision. Tied scores form one threshold. NaN when there are no positives.
    """
    y = np.asarray(y, dtype=float)
    score = np.asarray(score, dtype=float)
    positives = y.sum()
    if len(y) == 0 or positives == 0:
        return float("nan")
    order = np.argsort(-score, kind="stable")
    s, t = score[order], y[order]
    last = np.r_[np.flatnonzero(np.diff(s) != 0), len(s) - 1]
    tp = np.cumsum(t)[last]
    precision = tp / (last + 1)
    recall = tp / positives
    return float(np.sum(np.diff(np.r_[0.0, recall]) * precision))


def roc_auc(y: np.ndarray, score: np.ndarray) -> float:
    """Rank (Mann-Whitney) ROC-AUC with ties at half weight. NaN when one class is absent."""
    y = np.asarray(y, dtype=float)
    pos = int(y.sum())
    neg = len(y) - pos
    if pos == 0 or neg == 0:
        return float("nan")
    ranks = rankdata(np.asarray(score, dtype=float))
    return float((ranks[y == 1].sum() - pos * (pos + 1) / 2.0) / (pos * neg))


def brier(y: np.ndarray, p: np.ndarray) -> float:
    if len(y) == 0:
        return float("nan")
    return float(np.mean((np.asarray(p, dtype=float) - np.asarray(y, dtype=float)) ** 2))


def _ratio(num: float, den: float) -> float:
    return float(num / den) if den else float("nan")


def threshold_counts(y: np.ndarray, p: np.ndarray, threshold: float) -> dict[str, int]:
    pred = np.asarray(p, dtype=float) >= threshold
    truth = np.asarray(y, dtype=float) == 1
    return {
        "tp": int(np.sum(pred & truth)),
        "fp": int(np.sum(pred & ~truth)),
        "fn": int(np.sum(~pred & truth)),
        "tn": int(np.sum(~pred & ~truth)),
    }


def threshold_metrics(y: np.ndarray, p: np.ndarray, threshold: float) -> dict[str, float]:
    c = threshold_counts(y, p, threshold)
    precision = _ratio(c["tp"], c["tp"] + c["fp"])
    recall = _ratio(c["tp"], c["tp"] + c["fn"])
    f1 = _ratio(2 * c["tp"], 2 * c["tp"] + c["fp"] + c["fn"])
    return {"precision": precision, "recall": recall, "f1": f1}


def cost_threshold(c_fp: float, c_fn: float) -> float:
    """p* = C_FP / (C_FP + C_FN) (valid for calibrated probabilities)."""
    return float(c_fp / (c_fp + c_fn))


def mae(y: np.ndarray, pred: np.ndarray) -> float:
    if len(y) == 0:
        return float("nan")
    return float(np.mean(np.abs(np.asarray(pred, dtype=float) - np.asarray(y, dtype=float))))


def rmse(y: np.ndarray, pred: np.ndarray) -> float:
    if len(y) == 0:
        return float("nan")
    return float(
        np.sqrt(np.mean((np.asarray(pred, dtype=float) - np.asarray(y, dtype=float)) ** 2))
    )


def r2(y: np.ndarray, pred: np.ndarray) -> float:
    y = np.asarray(y, dtype=float)
    if len(y) < 2:
        return float("nan")
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    if ss_tot == 0:
        return float("nan")
    return float(1.0 - np.sum((y - np.asarray(pred, dtype=float)) ** 2) / ss_tot)


# ----------------------------------------------------------------------------- evaluation set
@dataclass
class EvalSet:
    """Out-of-sample rows of one evaluation (walk-forward or hold-out) and every prediction.

    event: `y` is 0/1 and `preds[name]` a probability.
    regression: `y` and `preds[name]` are on the transformed scale; `value` and
    `preds_value[name]` are in the original units; `censored` marks left-censored labels.
    """

    kind: str
    dates: np.ndarray
    stations: np.ndarray
    y: np.ndarray
    preds: dict[str, np.ndarray]
    value: np.ndarray | None = None
    preds_value: dict[str, np.ndarray] = field(default_factory=dict)
    censored: np.ndarray | None = None

    def __len__(self) -> int:
        return len(self.y)


def _event_metrics(es: EvalSet, name: str, idx: np.ndarray, thresholds: dict[str, float]):
    y, p = es.y[idx], es.preds[name][idx]
    out = {"pr_auc": average_precision(y, p), "roc_auc": roc_auc(y, p), "brier": brier(y, p)}
    if "base_rate" in es.preds:
        ref = brier(y, es.preds["base_rate"][idx])
        out["brier_skill_vs_base_rate"] = 1.0 - out["brier"] / ref if ref > 0 else float("nan")
    for label, thr in thresholds.items():
        for key, val in threshold_metrics(y, p, thr).items():
            out[f"{key}@{label}"] = val
    return out


def _regression_metrics(es: EvalSet, name: str, idx: np.ndarray, refs: list[str]):
    v, pv = es.value[idx], es.preds_value[name][idx]
    out = {
        "mae": mae(v, pv),
        "rmse": rmse(v, pv),
        "r2": r2(v, pv),
        "mae_log": mae(es.y[idx], es.preds[name][idx]),
    }
    for ref in refs:
        if ref in es.preds_value:
            ref_mae = mae(v, es.preds_value[ref][idx])
            out[f"skill_vs_{ref}"] = 1.0 - out["mae"] / ref_mae if ref_mae > 0 else float("nan")
    return out


def date_groups(dates: np.ndarray) -> list[np.ndarray]:
    """Row positions of each distinct date, in date order."""
    _, inverse = np.unique(np.asarray(dates), return_inverse=True)
    return [np.flatnonzero(inverse == g) for g in range(inverse.max() + 1)] if len(dates) else []


def bootstrap_indices(dates: np.ndarray, n_boot: int, seed: int) -> list[np.ndarray]:
    """`n_boot` resamples of the rows that draw whole dates with replacement."""
    groups = date_groups(dates)
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(n_boot):
        pick = rng.integers(0, len(groups), len(groups))
        out.append(np.concatenate([groups[g] for g in pick]))
    return out


def _interval(samples: np.ndarray, ci: float) -> tuple[float, float, int]:
    ok = samples[np.isfinite(samples)]
    if len(ok) == 0:
        return float("nan"), float("nan"), 0
    alpha = (1.0 - ci) / 2.0
    lo, hi = np.percentile(ok, [100 * alpha, 100 * (1 - alpha)])
    return float(lo), float(hi), int(len(ok))


def verdict(
    model: str,
    baseline: str,
    metric: str,
    diff: float,
    lo: float,
    hi: float,
    higher_is_better: bool,
    ci: float = 0.90,
) -> tuple[str, str]:
    """(code, sentence). Codes: beats, worse, no_difference, undefined."""
    pct = f"{round(ci * 100)}%"
    if not all(np.isfinite([diff, lo, hi])):
        return "undefined", f"{model} cannot be compared with {baseline} ({metric} is undefined)"
    detail = f"{metric} difference {diff:+.3f}, {pct} CI [{lo:+.3f}, {hi:+.3f}]"
    favourable = lo > 0 if higher_is_better else hi < 0
    unfavourable = hi < 0 if higher_is_better else lo > 0
    if favourable:
        return "beats", f"{model} beats {baseline} ({detail})"
    if unfavourable:
        return "worse", f"{model} is worse than {baseline} ({detail})"
    return (
        "no_difference",
        f"{model} shows no demonstrable difference from {baseline} "
        f"({metric} difference {diff:+.3f}, {pct} CI [{lo:+.3f}, {hi:+.3f}])",
    )


DIFF_METRICS = {
    # kind: (primary metric, higher is better), [(secondary metric, higher is better)]
    "event": (("pr_auc", True), [("brier", False)]),
    "regression": (("mae", False), []),
}
METRIC_LABEL = {"pr_auc": "PR-AUC", "brier": "Brier", "mae": "MAE"}


def evaluate(
    es: EvalSet,
    cfg: dict[str, Any],
    baselines: list[str],
    rows: np.ndarray | None = None,
    with_differences: bool = True,
    label: Callable[[str], str] = str,
) -> dict[str, Any]:
    """Point metrics, bootstrap intervals and paired differences on `rows` (default: all)."""
    idx_all = np.arange(len(es)) if rows is None else np.asarray(rows, dtype=int)
    ci = float(cfg.get("ci", 0.90))
    out: dict[str, Any] = {
        "n": int(len(idx_all)),
        "n_dates": int(len(np.unique(es.dates[idx_all]))),
        "predictors": {},
        "differences": [],
    }
    if es.kind == "event":
        positives = int(es.y[idx_all].sum())
        out["positives"] = positives
        out["base_rate"] = float(positives / len(idx_all)) if len(idx_all) else None
        if positives == 0:
            out["note"] = NO_POSITIVES
    else:
        out["n_censored"] = int(es.censored[idx_all].sum()) if es.censored is not None else 0
    if len(idx_all) == 0:
        return out

    costs = cfg.get("costs", {})
    thresholds = {"0.5": 0.5}
    if es.kind == "event" and costs:
        thresholds["p*"] = cost_threshold(float(costs["c_fp"]), float(costs["c_fn"]))
    refs = [b for b in baselines if b in es.preds]

    def point(name: str, idx: np.ndarray) -> dict[str, float]:
        if es.kind == "event":
            return _event_metrics(es, name, idx, thresholds)
        return _regression_metrics(es, name, idx, refs)

    names = list(es.preds)
    values = {name: point(name, idx_all) for name in names}
    metric_names = list(next(iter(values.values())))
    resamples = bootstrap_indices(es.dates[idx_all], int(cfg["n_boot"]), int(cfg["boot_seed"]))
    boot = {name: {m: np.full(len(resamples), np.nan) for m in metric_names} for name in names}
    for b, pick in enumerate(resamples):
        idx = idx_all[pick]
        for name in names:
            for m, val in point(name, idx).items():
                boot[name][m][b] = val

    for name in names:
        metrics = {}
        for m in metric_names:
            lo, hi, n_ok = _interval(boot[name][m], ci)
            metrics[m] = {"value": values[name][m], "lo": lo, "hi": hi, "n_boot_defined": n_ok}
        entry: dict[str, Any] = {"metrics": metrics}
        if es.kind == "event":
            entry["confusion"] = {
                lab: threshold_counts(es.y[idx_all], es.preds[name][idx_all], thr)
                for lab, thr in thresholds.items()
            }
        out["predictors"][name] = entry
    if es.kind == "event":
        out["thresholds"] = thresholds

    if with_differences:
        (primary, primary_hib), secondary = DIFF_METRICS[es.kind]
        for name in names:
            for base in refs:
                if base == name:
                    continue
                row: dict[str, Any] = {"model": name, "baseline": base}
                for metric, hib in [(primary, primary_hib), *secondary]:
                    d = values[name][metric] - values[base][metric]
                    lo, hi, _ = _interval(boot[name][metric] - boot[base][metric], ci)
                    code, sentence = verdict(
                        label(name), label(base), METRIC_LABEL[metric], d, lo, hi, hib, ci
                    )
                    row[metric] = {"diff": d, "lo": lo, "hi": hi, "verdict": code}
                    if metric == primary:
                        row["verdict"], row["sentence"] = code, sentence
                out["differences"].append(row)
    return out


def slice_rows(es: EvalSet, slices: dict[str, Any], by_station: bool) -> dict[str, np.ndarray]:
    """Row positions of each configured slice (month sets, then one slice per station)."""
    months = np.asarray(es.dates, dtype="datetime64[M]").astype(int) % 12 + 1
    out = {}
    for name, spec in (slices or {}).items():
        out[name] = np.flatnonzero(np.isin(months, list(spec["months"])))
    if by_station:
        for station in sorted(set(es.stations.tolist())):
            out[f"station {station}"] = np.flatnonzero(es.stations == station)
    return out
