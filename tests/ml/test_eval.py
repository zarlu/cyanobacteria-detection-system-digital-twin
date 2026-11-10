"""A6: metrics, date-level bootstrap, paired differences and the three verdict outcomes."""

from __future__ import annotations

import numpy as np
import pytest

from ml.eval import (
    NO_POSITIVES,
    EvalSet,
    average_precision,
    bootstrap_indices,
    brier,
    cost_threshold,
    date_groups,
    evaluate,
    mae,
    r2,
    rmse,
    roc_auc,
    slice_rows,
    threshold_counts,
    threshold_metrics,
    verdict,
)

CFG = {"n_boot": 300, "boot_seed": 7, "ci": 0.90, "costs": {"c_fp": 1, "c_fn": 9}}


def _dates(n, per_date=1, start="2020-01-01", step=11):
    base = np.datetime64(start)
    return np.repeat(base + np.arange(n) * np.timedelta64(step, "D"), per_date)


def test_average_precision_by_hand():
    # ranked: 1, 0, 1 -> precision at the positives 1/1 and 2/3, each a recall step of 1/2
    assert average_precision(np.array([1, 0, 1]), np.array([0.9, 0.8, 0.7])) == pytest.approx(5 / 6)
    # all scores tied: one threshold, precision = base rate
    assert average_precision(np.array([1, 0, 0, 1]), np.full(4, 0.3)) == pytest.approx(0.5)
    assert np.isnan(average_precision(np.array([0, 0]), np.array([0.1, 0.2])))


def test_roc_auc_by_hand():
    assert roc_auc(np.array([0, 0, 1, 1]), np.array([0.1, 0.4, 0.35, 0.8])) == pytest.approx(0.75)
    assert roc_auc(np.array([0, 1]), np.array([0.5, 0.5])) == pytest.approx(0.5)
    assert np.isnan(roc_auc(np.array([1, 1]), np.array([0.5, 0.6])))


def test_metrics_match_scikit_learn_including_ties():
    metrics = pytest.importorskip("sklearn.metrics")
    rng = np.random.default_rng(3)
    for _ in range(25):
        y = (rng.random(60) < 0.35).astype(float)
        if y.sum() in (0, len(y)):
            continue
        score = np.round(rng.random(60), 1)  # many ties
        assert average_precision(y, score) == pytest.approx(
            metrics.average_precision_score(y, score)
        )
        assert roc_auc(y, score) == pytest.approx(metrics.roc_auc_score(y, score))
        assert brier(y, score) == pytest.approx(metrics.brier_score_loss(y, score))


def test_threshold_metrics_and_cost_threshold():
    y = np.array([1, 1, 0, 0, 1])
    p = np.array([0.9, 0.2, 0.6, 0.05, 0.1])
    assert threshold_counts(y, p, 0.5) == {"tp": 1, "fp": 1, "fn": 2, "tn": 1}
    m = threshold_metrics(y, p, 0.5)
    assert (m["precision"], m["recall"], m["f1"]) == pytest.approx((0.5, 1 / 3, 0.4))
    assert cost_threshold(1, 9) == pytest.approx(0.1)
    assert threshold_counts(y, p, 0.1) == {"tp": 3, "fp": 1, "fn": 0, "tn": 1}  # p >= p* counts
    assert np.isnan(threshold_metrics(y, np.zeros(5), 0.5)["precision"])


def test_regression_metrics_by_hand():
    y, pred = np.array([1.0, 2.0, 3.0, 6.0]), np.array([2.0, 2.0, 1.0, 6.0])
    assert mae(y, pred) == pytest.approx(0.75)
    assert rmse(y, pred) == pytest.approx(np.sqrt(5 / 4))
    assert r2(y, pred) == pytest.approx(1 - 5 / 14)


def test_bootstrap_never_splits_a_date_and_is_seeded():
    dates = np.array(
        ["2020-01-01"] * 3 + ["2020-02-01"] * 1 + ["2020-03-01"] * 2, dtype="datetime64[D]"
    )
    groups = date_groups(dates)
    assert [g.tolist() for g in groups] == [[0, 1, 2], [3], [4, 5]]
    samples = bootstrap_indices(dates, 200, seed=1)
    seen_sizes = set()
    for idx in samples:
        seen_sizes.add(len(idx))
        for g in groups:
            counts = [int(np.sum(idx == row)) for row in g]
            assert len(set(counts)) == 1  # every row of a date is drawn the same number of times
        assert int(sum(np.sum(idx == g[0]) for g in groups)) == len(groups)  # 3 dates drawn
    assert len(seen_sizes) > 1  # rows per resample vary because whole dates are drawn
    again = bootstrap_indices(dates, 200, seed=1)
    assert all(np.array_equal(a, b) for a, b in zip(samples, again, strict=True))
    other = bootstrap_indices(dates, 200, seed=2)
    assert not all(np.array_equal(a, b) for a, b in zip(samples, other, strict=True))


@pytest.mark.parametrize(
    ("lo", "hi", "higher_is_better", "code", "words"),
    [
        (0.02, 0.20, True, "beats", "beats"),
        (-0.20, -0.02, True, "worse", "is worse than"),
        (-0.05, 0.09, True, "no_difference", "shows no demonstrable difference from"),
        (-0.90, -0.10, False, "beats", "beats"),  # lower MAE is better
        (0.10, 0.90, False, "worse", "is worse than"),
        (-0.30, 0.40, False, "no_difference", "shows no demonstrable difference from"),
        (0.0, 0.20, True, "no_difference", "shows no demonstrable difference from"),  # touches 0
    ],
)
def test_verdict_outcomes_on_constructed_intervals(lo, hi, higher_is_better, code, words):
    got, sentence = verdict(
        "model", "seasonal climatology", "PR-AUC", (lo + hi) / 2, lo, hi, higher_is_better
    )
    assert got == code
    assert sentence.startswith(f"model {words} seasonal climatology")
    assert "90% CI [" in sentence and f"{lo:+.3f}" in sentence and f"{hi:+.3f}" in sentence


def test_verdict_undefined_when_the_metric_is_undefined():
    code, sentence = verdict("m", "b", "PR-AUC", float("nan"), float("nan"), float("nan"), True)
    assert code == "undefined" and "cannot be compared" in sentence


def _event_set(n=120, seed=0):
    rng = np.random.default_rng(seed)
    y = (rng.random(n) < 0.4).astype(float)
    good = np.clip(y * 0.7 + rng.normal(0.15, 0.1, n), 0, 1)
    noise = rng.random(n)
    return EvalSet(
        kind="event",
        dates=_dates(n),
        stations=np.where(np.arange(n) % 2 == 0, "A", "B"),
        y=y,
        preds={
            "persistence": noise,
            "base_rate": np.full(n, 0.4),
            "good@set": good,
            "copy@set": noise.copy(),
            "bad@set": 1.0 - good,
        },
    )


def test_evaluate_event_differences_give_all_three_verdicts():
    out = evaluate(_event_set(), CFG, ["persistence", "base_rate"])
    by = {(d["model"], d["baseline"]): d for d in out["differences"]}
    assert by[("good@set", "persistence")]["verdict"] == "beats"
    assert by[("good@set", "persistence")]["brier"]["verdict"] == "beats"
    same = by[("copy@set", "persistence")]
    assert same["verdict"] == "no_difference"
    assert same["pr_auc"]["diff"] == 0 and same["pr_auc"]["lo"] == 0 and same["pr_auc"]["hi"] == 0
    bad = evaluate(
        _event_set(), CFG, ["good@set"]
    )  # use the good model as the reference: the bad one must be worse
    worse = {(d["model"], d["baseline"]): d for d in bad["differences"]}[("bad@set", "good@set")]
    assert worse["verdict"] == "worse" and "is worse than" in worse["sentence"]

    m = out["predictors"]["good@set"]["metrics"]
    assert m["pr_auc"]["lo"] <= m["pr_auc"]["value"] <= m["pr_auc"]["hi"]
    assert out["predictors"]["base_rate"]["metrics"]["brier_skill_vs_base_rate"]["value"] == 0
    assert m["brier_skill_vs_base_rate"]["value"] > 0
    assert set(out["predictors"]["good@set"]["confusion"]) == {"0.5", "p*"}
    assert out["thresholds"]["p*"] == pytest.approx(0.1)
    assert out["positives"] == int(_event_set().y.sum())


def test_slice_without_positives_is_reported_as_undefined():
    es = _event_set()
    rows = np.flatnonzero(es.y == 0)[:20]
    out = evaluate(es, CFG, ["persistence"], rows=rows, with_differences=False)
    assert out["positives"] == 0 and out["note"] == NO_POSITIVES
    assert np.isnan(out["predictors"]["good@set"]["metrics"]["pr_auc"]["value"])
    assert np.isfinite(out["predictors"]["good@set"]["metrics"]["brier"]["value"])
    assert out["differences"] == []


def test_evaluate_regression_skill_and_numeric_only_rows():
    rng = np.random.default_rng(1)
    n = 80
    value = np.exp(rng.normal(1.5, 0.6, n))
    censored = np.arange(n) % 5 == 0
    preds_value = {
        "persistence": value * np.exp(rng.normal(0, 0.8, n)),
        "seasonal_climatology": np.full(n, float(np.mean(value))),
        "good@set": value * np.exp(rng.normal(0, 0.05, n)),
    }
    es = EvalSet(
        kind="regression",
        dates=_dates(n),
        stations=np.full(n, "A"),
        y=np.log1p(value),
        preds={k: np.log1p(v) for k, v in preds_value.items()},
        value=value,
        preds_value=preds_value,
        censored=censored,
    )
    out = evaluate(es, CFG, ["persistence", "seasonal_climatology"])
    m = out["predictors"]["good@set"]["metrics"]
    expected = 1 - mae(value, preds_value["good@set"]) / mae(value, preds_value["persistence"])
    assert m["skill_vs_persistence"]["value"] == pytest.approx(expected)
    assert m["mae"]["value"] == pytest.approx(mae(value, preds_value["good@set"]))
    assert m["mae_log"]["value"] == pytest.approx(mae(es.y, es.preds["good@set"]))
    assert out["predictors"]["persistence"]["metrics"]["skill_vs_persistence"]["value"] == 0
    by = {(d["model"], d["baseline"]): d for d in out["differences"]}
    assert by[("good@set", "persistence")]["verdict"] == "beats"
    assert by[("good@set", "seasonal_climatology")]["mae"]["hi"] < 0
    assert out["n_censored"] == int(censored.sum())
    numeric = evaluate(es, CFG, ["persistence"], rows=np.flatnonzero(~censored))
    assert numeric["n"] == n - int(censored.sum()) and numeric["n_censored"] == 0


def test_slice_rows_by_month_and_station():
    es = _event_set(n=40)
    slices = {
        "apr_sep": {"months": [4, 5, 6, 7, 8, 9]},
        "oct_mar": {"months": [10, 11, 12, 1, 2, 3]},
    }
    rows = slice_rows(es, slices, by_station=True)
    months = es.dates.astype("datetime64[M]").astype(int) % 12 + 1
    assert rows["apr_sep"].tolist() == np.flatnonzero((months >= 4) & (months <= 9)).tolist()
    assert len(rows["apr_sep"]) + len(rows["oct_mar"]) == 40
    assert rows["station A"].tolist() == list(range(0, 40, 2))
    assert "station A" not in slice_rows(es, slices, by_station=False)
