"""A2. Config-driven feature sets and fold-local imputation.

Every feature of a row dated `d` uses only rows dated `<= d`:
  * same-visit measurements are same-date readings ("nowcast" features);
  * previous-label features use only labels with a date strictly before `d`.
`ml.drills.truncation_check` proves this for every issue date by rebuilding the features with
all later rows removed.

Missing values are imputed inside each fold with the training-fold median, plus a
missing-indicator column for every feature that has a missing value in train or test.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np
import pandas as pd

from ml.config import TaskError, resolve_feature_set

DAYS_PER_YEAR = 365.25


class FeatureError(Exception):
    """A feature spec cannot be built."""


def previous_by_station(
    station: pd.Series, date: pd.Series, value: pd.Series
) -> tuple[pd.Series, pd.Series]:
    """For every row: the latest non-missing `value` at the same station on a strictly earlier
    date, and the number of days since it. Rows must be unique per (station, date).
    """
    df = pd.DataFrame({"station": station, "date": date, "value": value})
    order = df.sort_values(["station", "date"], kind="stable")
    has = order["value"].notna()
    known_date = order["date"].where(has)
    grouped = order["station"]
    prev_value = order["value"].groupby(grouped).shift(1).groupby(grouped).ffill()
    prev_date = known_date.groupby(grouped).shift(1).groupby(grouped).ffill()
    days = (order["date"] - prev_date).dt.total_seconds() / 86400.0
    return prev_value.reindex(df.index), days.reindex(df.index)


def _apply_transform(values: pd.Series, name: str | None) -> pd.Series:
    v = values.astype(float)
    if name in (None, "none"):
        return v
    if name == "log":
        return np.log(v.where(v > 0))
    if name == "log1p":
        return np.log1p(v.where(v >= 0))
    raise FeatureError(f"unknown feature transform {name!r}")


def _season(frame: pd.DataFrame, fn: Callable) -> pd.Series:
    angle = 2.0 * np.pi * frame["date"].dt.day_of_year.astype(float) / DAYS_PER_YEAR
    return fn(angle)


def _f_season_sin(frame, spec, ctx):
    return {spec["name"]: _season(frame, np.sin)}


def _f_season_cos(frame, spec, ctx):
    return {spec["name"]: _season(frame, np.cos)}


def _f_channel(frame, spec, ctx):
    channel = spec["channel"]
    if channel == ctx["target"]:
        raise FeatureError("the target channel cannot be a same-visit feature")
    missing = pd.Series(np.nan, index=frame.index)
    values = frame[channel] if channel in frame.columns else missing
    return {spec["name"]: _apply_transform(values, spec.get("transform"))}


def _f_station_onehot(frame, spec, ctx):
    stations = ctx["stations"]
    if len(stations) < 2:
        return {}
    return {f"{spec['name']}_{s}": (frame["station"] == s).astype(float) for s in stations}


def _f_prev_label(frame, spec, ctx):
    prev, _ = previous_by_station(frame["station"], frame["date"], frame[ctx["target"]])
    return {spec["name"]: _apply_transform(prev, spec.get("transform"))}


def _f_prev_label_days(frame, spec, ctx):
    _, days = previous_by_station(frame["station"], frame["date"], frame[ctx["target"]])
    return {spec["name"]: days.astype(float)}


FEATURE_KINDS: dict[str, Callable] = {
    "season_sin": _f_season_sin,
    "season_cos": _f_season_cos,
    "channel": _f_channel,
    "station_onehot": _f_station_onehot,
    "prev_label": _f_prev_label,
    "prev_label_days": _f_prev_label_days,
}


def build_features(frame: pd.DataFrame, task: dict[str, Any], set_name: str) -> pd.DataFrame:
    """Feature matrix for every (station, date) row of `frame` (same index as `frame`)."""
    specs, _ = resolve_feature_set(task, set_name)
    ctx = {
        "target": task["target"]["channel"],
        "stations": [str(s) for s in task["data"]["stations"]],
    }
    columns: dict[str, pd.Series] = {}
    for spec in specs:
        kind = spec.get("kind")
        if kind not in FEATURE_KINDS:
            raise TaskError(f"unknown feature kind {kind!r} in set {set_name!r}")
        columns.update(FEATURE_KINDS[kind](frame, spec, ctx))
    out = pd.DataFrame(columns, index=frame.index).astype(float)
    return out.replace([np.inf, -np.inf], np.nan)


def complete_case_share(X: pd.DataFrame, dates: pd.Series, last_years: int = 2) -> dict[str, Any]:
    """Share of label rows with no missing feature, overall and in the last calendar years."""
    complete = X.notna().all(axis=1).to_numpy()
    years = pd.DatetimeIndex(dates).year.to_numpy()
    recent_years = sorted(set(years.tolist()))[-last_years:] if len(years) else []
    recent = np.isin(years, recent_years)
    missing_share = X.isna().mean()
    return {
        "n": int(len(X)),
        "complete_share": float(complete.mean()) if len(X) else None,
        "recent_years": [int(y) for y in recent_years],
        "recent_n": int(recent.sum()),
        "recent_complete_share": float(complete[recent].mean()) if recent.any() else None,
        "missing_share_by_feature": {str(c): float(v) for c, v in missing_share.items() if v > 0},
    }


class FoldImputer:
    """Median imputation with statistics from the training fold only.

    `fit(X_train, X_other)`: medians come from `X_train` alone. `X_other` (the test rows of the
    fold) is looked at only to decide which features get a missing-indicator column: its values
    never enter a statistic. A feature that is entirely missing in train is filled with 0.
    """

    def fit(self, X_train: pd.DataFrame, X_other: pd.DataFrame | None = None) -> FoldImputer:
        self.columns_ = list(X_train.columns)
        medians = X_train.median(axis=0, skipna=True)
        self.medians_ = medians.fillna(0.0)
        self.n_fit_rows_ = int(len(X_train))
        missing = X_train.isna().any(axis=0)
        if X_other is not None:
            missing = missing | X_other[self.columns_].isna().any(axis=0)
        self.indicator_columns_ = [c for c in self.columns_ if bool(missing[c])]
        self.output_columns_ = self.columns_ + [f"{c}__missing" for c in self.indicator_columns_]
        return self

    def transform(self, X: pd.DataFrame) -> pd.DataFrame:
        X = X[self.columns_]
        out = X.fillna(self.medians_)
        for c in self.indicator_columns_:
            out[f"{c}__missing"] = X[c].isna().astype(float)
        return out[self.output_columns_]
