"""A3. Chronological splits only: walk-forward by calendar year, then one final hold-out.

Units are distinct sample dates: all rows of one date are always on the same side.
  * Final hold-out: the labels on the last `final_holdout_frac` of distinct dates. They are
    excluded from every walk-forward fold. Train = labels dated before
    (hold-out start - gap_days).
  * Walk-forward, test year Y: test = labels dated in Y (before the hold-out start);
    train = labels dated before (Y-01-01 - gap_days).
A fold with too few training labels (or positives, for event tasks) is skipped and reported.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd


class SplitError(Exception):
    """A split breaks chronology."""


@dataclass(frozen=True)
class Fold:
    name: str
    train_idx: np.ndarray
    test_idx: np.ndarray
    boundary: pd.Timestamp  # first instant of the test window
    gap_days: int

    def describe(self, labels: pd.DataFrame, kind: str) -> dict[str, Any]:
        tr, te = labels.iloc[self.train_idx], labels.iloc[self.test_idx]
        out: dict[str, Any] = {
            "fold": self.name,
            "n_train": int(len(tr)),
            "n_test": int(len(te)),
            "train_last_date": str(tr["date"].max().date()) if len(tr) else None,
            "test_first_date": str(te["date"].min().date()) if len(te) else None,
            "test_last_date": str(te["date"].max().date()) if len(te) else None,
        }
        if kind == "event":
            out["train_positives"] = int(tr["y"].sum())
            out["test_positives"] = int(te["y"].sum())
        return out


@dataclass
class SplitPlan:
    folds: list[Fold] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    holdout: Fold | None = None
    holdout_start: pd.Timestamp | None = None
    holdout_skipped: str | None = None
    n_holdout_dates: int = 0


def _skip_reason(train: pd.DataFrame, cfg: dict[str, Any], kind: str) -> str | None:
    min_labels = int(cfg.get("min_train_labels", 1))
    if len(train) < min_labels:
        return f"{len(train)} training labels, fewer than min_train_labels={min_labels}"
    if kind == "event":
        min_pos = int(cfg.get("min_train_positives", 0))
        pos = int(train["y"].sum())
        if pos < min_pos:
            return f"{pos} training positives, fewer than min_train_positives={min_pos}"
    return None


def make_splits(labels: pd.DataFrame, cfg: dict[str, Any], kind: str) -> SplitPlan:
    """Build the walk-forward folds and the final hold-out for a label table.

    `labels` needs a RangeIndex and the columns `date` and `y`.
    """
    plan = SplitPlan()
    if labels.empty:
        return plan
    gap = int(cfg.get("gap_days", 0))
    gap_td = pd.Timedelta(days=gap)
    dates = labels["date"]
    distinct = np.sort(dates.unique())
    frac = float(cfg.get("final_holdout_frac", 0.0))

    n_hold = min(len(distinct) - 1, math.ceil(frac * len(distinct))) if frac > 0 else 0
    in_holdout = np.zeros(len(labels), dtype=bool)
    if n_hold > 0:
        plan.holdout_start = pd.Timestamp(distinct[-n_hold])
        plan.n_holdout_dates = int(n_hold)
        in_holdout = (dates >= plan.holdout_start).to_numpy()
        train_mask = (dates < plan.holdout_start - gap_td).to_numpy()
        reason = _skip_reason(labels[train_mask], cfg, kind)
        if reason:
            plan.holdout_skipped = reason
        else:
            plan.holdout = Fold(
                "holdout",
                np.flatnonzero(train_mask),
                np.flatnonzero(in_holdout),
                plan.holdout_start,
                gap,
            )

    pre = labels[~in_holdout]
    if pre.empty:
        return plan
    years = dates.dt.year.to_numpy()
    last_year = int(pre["date"].dt.year.max())
    for year in range(int(cfg["first_test_year"]), last_year + 1):
        boundary = pd.Timestamp(year=year, month=1, day=1)
        test_mask = (years == year) & ~in_holdout
        train_mask = (dates < boundary - gap_td).to_numpy()
        if not test_mask.any():
            plan.skipped.append({"fold": str(year), "reason": "no test labels in this year"})
            continue
        reason = _skip_reason(labels[train_mask], cfg, kind)
        if reason:
            plan.skipped.append(
                {"fold": str(year), "reason": reason, "n_test_not_scored": int(test_mask.sum())}
            )
            continue
        fold = Fold(str(year), np.flatnonzero(train_mask), np.flatnonzero(test_mask), boundary, gap)
        plan.folds.append(fold)

    for fold in [*plan.folds, *([plan.holdout] if plan.holdout else [])]:
        assert_chronological(labels["date"], fold)
    return plan


def assert_chronological(dates: pd.Series, fold: Fold) -> None:
    """Raise SplitError unless every train date is before every test date, no date is on both
    sides, and the gap between the last train date and the test window is respected.
    """
    train = dates.iloc[fold.train_idx]
    test = dates.iloc[fold.test_idx]
    if len(train) == 0 or len(test) == 0:
        raise SplitError(f"fold {fold.name}: empty train or test side")
    if set(train.unique()) & set(test.unique()):
        raise SplitError(f"fold {fold.name}: a date appears in both train and test")
    if train.max() >= test.min():
        raise SplitError(f"fold {fold.name}: a train date is at or after a test date")
    if test.min() < fold.boundary:
        raise SplitError(f"fold {fold.name}: a test date is before the fold boundary")
    if train.max() >= fold.boundary - pd.Timedelta(days=fold.gap_days):
        raise SplitError(f"fold {fold.name}: the {fold.gap_days}-day gap is not respected")
