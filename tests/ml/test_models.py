"""A5: one interface (fit, predict, predict_interval, explain) for the whole ladder."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("sklearn")
pytest.importorskip("lightgbm")

from ml.eval import average_precision, mae  # noqa: E402
from ml.models import FoldModel, conformal_quantile, permutation_importance  # noqa: E402

TREES = {"n_estimators": 20, "min_samples_leaf": 3}
BOOST = {"num_leaves": 7, "min_child_samples": 5, "n_estimators": 20}
EVENT = {"logistic": {"C": 0.5}, "random_forest": TREES, "lightgbm": BOOST}
REGRESSION = {"ridge": {"alpha": 10}, "random_forest": TREES, "lightgbm": BOOST}


def _data(n=160, seed=0):
    rng = np.random.default_rng(seed)
    X = pd.DataFrame({"signal": rng.normal(size=n), "noise": rng.normal(size=n)})
    X.loc[rng.random(n) < 0.1, "noise"] = np.nan
    y_reg = 2.0 + 1.5 * X["signal"].to_numpy() + rng.normal(0, 0.3, n)
    y_evt = (X["signal"].to_numpy() + rng.normal(0, 0.5, n) > 0).astype(float)
    return X, y_evt, y_reg


@pytest.mark.parametrize("algo", list(EVENT))
def test_event_models_share_the_interface(algo):
    X, y, _ = _data()
    model = FoldModel(algo, "event", EVENT[algo], seed=7).fit(X[:120], y[:120], X[120:])
    p = model.predict(X[120:])
    assert p.shape == (40,) and np.all((p >= 0) & (p <= 1))
    assert average_precision(y[120:], p) > y[120:].mean() + 0.2  # far above the base rate
    again = FoldModel(algo, "event", EVENT[algo], seed=7).fit(X[:120], y[:120], X[120:])
    assert np.array_equal(p, again.predict(X[120:]))  # same seed, same predictions
    with pytest.raises(NotImplementedError):
        model.predict_interval(X[120:])
    imp = model.explain(X[120:], y[120:], average_precision, n_repeats=3, seed=1)
    assert set(imp) == {"signal", "noise"} and imp["signal"] > imp["noise"]


@pytest.mark.parametrize("algo", list(REGRESSION))
def test_regression_models_share_the_interface(algo):
    X, _, y = _data()
    model = FoldModel(algo, "regression", REGRESSION[algo], seed=7).fit(X[:120], y[:120], X[120:])
    pred = model.predict(X[120:])
    assert pred.shape == (40,) and mae(y[120:], pred) < mae(y[120:], np.full(40, y[:120].mean()))
    with pytest.raises(RuntimeError, match="calibrate"):
        model.predict_interval(X[120:])
    model.calibrate(np.array([0.1, -0.2, 0.3, -0.4, 0.5, 0.6, -0.7, 0.8, 0.9]), level=0.90)
    lo, hi = model.predict_interval(X[120:])
    assert model.q_ == pytest.approx(0.9)
    assert np.allclose(hi - lo, 1.8) and np.allclose((lo + hi) / 2, pred)
    imp = model.explain(X[120:], y[120:], lambda a, b: -mae(a, b), n_repeats=3, seed=1)
    assert imp["signal"] > imp["noise"]


def test_model_imputes_with_training_medians_only():
    X, y, _ = _data()
    train, test = X[:120], X[120:].copy()
    test["noise"] = 1e6  # absurd test values must not move the training statistic
    model = FoldModel("logistic", "event", EVENT["logistic"], seed=7).fit(train, y[:120], test)
    assert model.imputer_.medians_["noise"] == pytest.approx(train["noise"].median())
    assert model.imputer_.n_fit_rows_ == 120


def test_one_class_in_training_predicts_its_rate():
    X, _, _ = _data(40)
    model = FoldModel("logistic", "event", EVENT["logistic"], seed=7).fit(X, np.zeros(40))
    assert model.predict(X).tolist() == [0.0] * 40


def test_unknown_algorithm_is_an_error():
    X, y, _ = _data(40)
    with pytest.raises(ValueError, match="no algorithm"):
        FoldModel("ridge", "event", {}, seed=7).fit(X, y)


def test_conformal_quantile_by_hand():
    r = np.array([0.1, -0.2, 0.3, -0.4, 0.5, 0.6, -0.7, 0.8, 0.9])
    assert conformal_quantile(r, 0.90) == pytest.approx(0.9)  # ceil(10 * 0.9) = 9th smallest
    assert conformal_quantile(np.arange(1, 20), 0.90) == 18  # ceil(20 * 0.9) = 18th of 19
    assert conformal_quantile(r[:5], 0.90) == float("inf")  # ceil(6 * 0.9) = 6 > 5: too few
    assert conformal_quantile(np.array([]), 0.90) == float("inf")


def test_permutation_importance_pools_folds():
    X, y, _ = _data()
    m1 = FoldModel("logistic", "event", EVENT["logistic"], 7).fit(X[:80], y[:80])
    m2 = FoldModel("logistic", "event", EVENT["logistic"], 7).fit(X[:120], y[:120])
    fitted = [(m1, X[80:120], y[80:120]), (m2, X[120:], y[120:])]
    a = permutation_importance(fitted, average_precision, n_repeats=3, seed=5)
    b = permutation_importance(fitted, average_precision, n_repeats=3, seed=5)
    assert a == b and a["signal"] > 0.1
