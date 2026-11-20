"""Benchmarks are copied from metrics.json, never recomputed; FAST reports are not served."""

from __future__ import annotations

import json

from ml.benchmarks import REPORTS, benchmarks, latest_report, report_tasks

TASK = "crp_chla_demo"


def test_values_equal_the_committed_metrics_file():
    body = benchmarks(TASK)
    path = latest_report(TASK)
    metrics = json.loads((path / "metrics.json").read_text())
    assert body["report_date"] == path.name and body["primary_target"] == "event"
    for kind, metric in (("event", "pr_auc"), ("regression", "mae")):
        for block in ("walk_forward", "holdout"):
            src = metrics["targets"][kind][block]["all"]
            got = body["kinds"][kind]["blocks"][block]
            assert got["n"] == src["n"]
            assert [r["key"] for r in got["rows"]] == list(src["predictors"])
            for row in got["rows"]:
                m = src["predictors"][row["key"]]["metrics"][metric]
                assert (row["value"], row["lo"], row["hi"]) == (m["value"], m["lo"], m["hi"])


def test_every_model_row_carries_its_verdict_against_each_baseline():
    block = benchmarks(TASK)["kinds"]["event"]["blocks"]["walk_forward"]
    baselines = {r["key"] for r in block["rows"] if r["is_baseline"]}
    assert {"persistence", "seasonal_climatology", "station_climatology"} <= baselines
    models = [r for r in block["rows"] if not r["is_baseline"]]
    assert {r["algorithm"] for r in models} == {"logistic", "random_forest", "lightgbm", "xgboost"}
    for row in models:
        assert {"persistence", "seasonal_climatology", "station_climatology"} <= set(
            row["verdicts"]
        )
        for v in row["verdicts"].values():
            assert v["verdict"] in ("beats", "worse", "no_difference") and v["sentence"]


def test_fast_reports_and_unknown_tasks_are_not_served(tmp_path):
    fast = tmp_path / "t" / "2026-01-02-FAST"
    fast.mkdir(parents=True)
    (fast / "metrics.json").write_text("{}")
    assert latest_report("t", tmp_path) is None and report_tasks(tmp_path) == []
    assert benchmarks("nope", tmp_path) is None
    assert TASK in report_tasks(REPORTS)
