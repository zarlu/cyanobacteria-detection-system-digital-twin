"""Report figures, drawn from the result dict only (no recomputation).

Per target kind: the target by calendar month with the out-of-sample predictions overlaid,
the headline metric of every predictor with its bootstrap interval, and permutation importance
for the headline feature set. Every axis has a title with units.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e3df"
NEUTRAL = "#a9a8a2"
NEUTRAL_DARK = "#52514e"
# colour follows the algorithm, in a fixed order (never the rank)
ALGO_COLOR = {
    "logistic": "#2a78d6",
    "ridge": "#2a78d6",
    "random_forest": "#eb6834",
    "lightgbm": "#1baf7a",
}
BASELINE_STYLE = {
    "seasonal_climatology": (NEUTRAL_DARK, "--", "s"),
    "persistence": (NEUTRAL, ":", "^"),
    "base_rate": (NEUTRAL, "-.", "v"),
}
MONTHS = ["J", "F", "M", "A", "M", "J", "J", "A", "S", "O", "N", "D"]


def _algo(key: str) -> str:
    return key.split("@", 1)[0]


def _style(ax) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_2, labelsize=9)
    ax.xaxis.label.set_color(INK_2)
    ax.yaxis.label.set_color(INK_2)
    ax.title.set_color(INK)


def _save(fig, path: Path) -> Path:
    fig.patch.set_facecolor(SURFACE)
    fig.tight_layout()
    fig.savefig(path, dpi=130, metadata={"Software": None})
    plt.close(fig)
    return path


def _by_month(result: dict[str, Any], kind: str, out_dir: Path) -> Path | None:
    res = result["targets"][kind]
    profile = res.get("month_profile")
    if not profile:
        return None
    unit = result["target"].get("unit", "")
    channel = result["target"]["channel"]
    x = list(range(1, 13))
    fig, ax = plt.subplots(figsize=(8.2, 5.4))
    _style(ax)
    if kind == "event":
        labels = profile["labels"]
        rate = [
            labels[str(m)]["positives"] / labels[str(m)]["n"] if labels[str(m)]["n"] else 0.0
            for m in x
        ]
        ax.bar(
            x, rate, width=0.72, color="#d9d8d3", label="observed share of positives (all labels)"
        )
        tick_labels = [
            f"{MONTHS[m - 1]}\n{labels[str(m)]['positives']}/{labels[str(m)]['n']}" for m in x
        ]
        xlabel = "calendar month of the sample (below: positives / labels, all labels)"
        ax.set_ylim(0, 1.05)
        thr = result["target"]["event_threshold"]
        ax.set_ylabel(f"probability that {channel} >= {thr} {unit} (0 to 1)")
        title = "Event probability by calendar month: outcomes and walk-forward predictions"
    else:
        wf = profile["walk_forward"]
        obs = [wf[str(m)].get("observed_mean", 0.0) for m in x]
        ax.bar(x, obs, width=0.72, color="#d9d8d3", label="observed mean (walk-forward labels)")
        ax.set_ylabel(f"{channel} ({unit})")
        title = "Target by calendar month: observed mean and walk-forward predictions"
        tick_labels = [f"{MONTHS[m - 1]}\n{wf[str(m)]['n']}" for m in x]
        xlabel = "calendar month of the sample (below: walk-forward labels)"
    wf = profile["walk_forward"]
    keys: list[str] = []
    for m in x:
        for k in wf[str(m)].get("predicted_mean", {}):
            if k not in keys:
                keys.append(k)
    for key in keys:
        xs = [m for m in x if key in wf[str(m)].get("predicted_mean", {})]
        ys = [wf[str(m)]["predicted_mean"][key] for m in xs]
        if key in BASELINE_STYLE:
            if key == "base_rate":
                continue
            color, ls, marker = BASELINE_STYLE[key]
        else:
            color, ls, marker = ALGO_COLOR.get(_algo(key), INK), "-", "o"
        ax.plot(xs, ys, ls=ls, marker=marker, ms=6, lw=2, color=color,
                markeredgecolor=SURFACE, markeredgewidth=1.2,
                label=f"{key.replace('_', ' ')} (mean prediction)")  # fmt: skip
    ax.set_xticks(x, tick_labels)
    ax.set_xlabel(xlabel)
    ax.set_title(title, fontsize=11, loc="left")
    ax.yaxis.grid(True, color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    ax.legend(
        fontsize=8,
        frameon=False,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.24),
        ncol=2,
        labelcolor=INK_2,
    )
    return _save(fig, out_dir / f"{kind}_by_month.png")


def _metric_bars(result: dict[str, Any], kind: str, out_dir: Path) -> Path | None:
    block = result["targets"][kind].get("walk_forward")
    if not block:
        return None
    metric = "pr_auc" if kind == "event" else "mae"
    unit = result["target"].get("unit", "")
    names, vals, los, his, colors = [], [], [], [], []
    for key, entry in block["all"]["predictors"].items():
        m = entry["metrics"][metric]
        if m["value"] is None:
            continue
        names.append(key.replace("_", " "))
        vals.append(m["value"])
        los.append(m["value"] - (m["lo"] if m["lo"] is not None else m["value"]))
        his.append((m["hi"] if m["hi"] is not None else m["value"]) - m["value"])
        colors.append(NEUTRAL if "@" not in key else ALGO_COLOR.get(_algo(key), INK))
    if not names:
        return None
    fig, ax = plt.subplots(figsize=(8.2, 0.36 * len(names) + 1.6))
    _style(ax)
    y = list(range(len(names)))[::-1]
    ax.barh(y, vals, height=0.62, color=colors, edgecolor=SURFACE, linewidth=2)
    ax.errorbar(vals, y, xerr=[los, his], fmt="none", ecolor=INK, elinewidth=1.2, capsize=3)
    ax.set_yticks(y, names)
    for yi, v, hi in zip(y, vals, his, strict=True):
        ax.text(v + hi + 0.01 * max(vals), yi, f"{v:.3f}", va="center", fontsize=8, color=INK_2)
    if kind == "event":
        ax.set_xlabel("PR-AUC, walk-forward (0 to 1; higher is better); bars: 90% bootstrap CI")
        base = block["all"].get("base_rate")
        if base is not None:
            ax.axvline(base, color=INK_2, lw=1, ls=":")
            ax.text(base, len(names) - 0.4, " base rate", fontsize=8, color=INK_2)
    else:
        ax.set_xlabel(f"MAE, walk-forward ({unit}; lower is better); bars: 90% bootstrap CI")
    ax.set_ylabel("predictor @ feature set (grey = baseline)")
    ax.set_title("Baselines first, then models", fontsize=11, loc="left")
    ax.xaxis.grid(True, color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    return _save(fig, out_dir / f"{kind}_metric_bars.png")


def _importance(result: dict[str, Any], kind: str, out_dir: Path) -> Path | None:
    imp = result["targets"][kind].get("importance")
    if not imp:
        return None
    headline = result["headline_feature_set"]
    keys = [k for k in imp["predictors"] if k.endswith(f"@{headline}")]
    if not keys:
        return None
    n_feat = len(imp["predictors"][keys[0]])
    fig, axes = plt.subplots(
        1, len(keys), figsize=(4.0 * len(keys), 0.3 * n_feat + 1.8), sharey=True
    )
    axes = [axes] if len(keys) == 1 else list(axes)
    order = sorted(imp["predictors"][keys[0]], key=lambda f: imp["predictors"][keys[0]][f])
    for ax, key in zip(axes, keys, strict=True):
        _style(ax)
        vals = [imp["predictors"][key][f] for f in order]
        ax.barh(order, vals, height=0.62, color=ALGO_COLOR.get(_algo(key), INK))
        ax.axvline(0, color=INK_2, lw=0.8)
        ax.set_title(_algo(key).replace("_", " "), fontsize=10, loc="left")
        what = "PR-AUC" if kind == "event" else "log-scale MAE"
        ax.set_xlabel(f"loss of {what} when shuffled")
        ax.xaxis.grid(True, color=GRID, lw=0.8)
        ax.set_axisbelow(True)
    axes[0].set_ylabel(f"feature ({headline})")
    return _save(fig, out_dir / f"{kind}_importance.png")


def write_figures(result: dict[str, Any], out_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for kind in result["targets"]:
        for maker in (_by_month, _metric_bars, _importance):
            path = maker(result, kind, out_dir)
            if path is not None:
                paths.append(path)
    return paths
