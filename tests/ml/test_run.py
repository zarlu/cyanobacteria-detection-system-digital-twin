"""A8: the runner end to end on invented data, determinism, and one real-data integration test."""

from __future__ import annotations

import hashlib
import json

import pytest

pytest.importorskip("sklearn")
pytest.importorskip("lightgbm")
pytest.importorskip("matplotlib")

from ml.config import REPO, load_task  # noqa: E402
from ml.report import render_report  # noqa: E402
from ml.run import main, merge_warnings, run_task, write_outputs  # noqa: E402
from ml.source import data_path  # noqa: E402

REAL_DATA = REPO / "data" / "clean" / "crp_long.csv"


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    from tests.ml.conftest import build_long, build_task

    path = tmp_path_factory.mktemp("run") / "long.csv"
    build_long().to_csv(path, index=False)
    task = build_task(path)
    return task, run_task(task)


def test_two_runs_give_identical_metrics_json(result, tmp_path):
    task, first = result
    second = run_task(task)
    a = write_outputs(first, tmp_path / "a", figures=False)[0]
    b = write_outputs(second, tmp_path / "b", figures=False)[0]
    assert a.name == b.name == "metrics.json"
    sha = [hashlib.sha256(p.read_bytes()).hexdigest() for p in (a, b)]
    assert sha[0] == sha[1]
    json.loads(a.read_text(encoding="utf-8"))  # strict JSON: no NaN or Infinity
    assert "NaN" not in a.read_text(encoding="utf-8")


def test_result_has_every_part_of_the_brief(result):
    _, res = result
    assert res["fast"] is True and res["source"] == "CRP" and len(res["input"]["sha256"]) == 64
    assert list(res["targets"]) == ["event", "regression"]
    event = res["targets"]["event"]
    wf = event["walk_forward"]["all"]
    names = list(wf["predictors"])
    assert names[:3] == ["persistence", "seasonal_climatology", "base_rate"]  # baselines first
    assert len(names) == 3 + 3 * 3
    assert wf["n"] == sum(f["n_test"] for f in event["folds"])
    assert event["holdout"]["all"]["n"] == event["holdout_fold"]["n_test"]
    assert set(event["walk_forward"]["slices"]) == {
        "apr_sep",
        "oct_mar",
        "station 90001",
        "station 90002",
    }
    for d in wf["differences"]:
        assert d["verdict"] in {"beats", "worse", "no_difference", "undefined"}
        assert d["baseline"] in {"persistence", "seasonal_climatology", "base_rate"}
    lk = event["leakage_checks"]
    assert all(t["passed"] for t in lk["truncation"]) and lk["shuffled_labels"]["ran"]
    assert "NOT a valid estimate" in lk["random_split_demo"]["label"]
    assert set(event["feature_availability"]) == {
        "sensor_like",
        "sensor_like_prev",
        "lab_same_visit",
    }
    assert event["feature_availability"]["lab_same_visit"]["deployable"] is False
    reg = res["targets"]["regression"]
    assert "numeric_only" in reg["walk_forward"] and reg["conformal"]["level"] == 0.9
    assert reg["walk_forward"]["numeric_only"]["n_censored"] == 0
    assert set(event["importance"]["predictors"]) == set(names[3:])


def test_walk_forward_and_holdout_never_share_a_label(result):
    _, res = result
    event = res["targets"]["event"]
    assert event["labels"]["labels_used"] >= (
        event["walk_forward"]["all"]["n"] + event["holdout"]["all"]["n"]
    )
    for fold in event["folds"]:
        assert fold["train_last_date"] < fold["test_first_date"]
        assert fold["test_last_date"] < event["holdout_start"]


def test_report_first_lines_and_sections(result):
    _, res = result
    report = render_report(res, ["event_by_month.png"])
    lines = report.splitlines()
    assert lines[0] == "# Report: test_task (FAST)"
    assert "FAST run: development only" in lines[2]
    top = report.split("## Warnings")[0]
    for needle in (
        "### Primary target: event chlorophyll_a >= 10 ug/L",
        "- Walk-forward:",
        "n_train from",
        "n_test =",
        "n_holdout =",
        "positives",
        "**Persistence** means: the last sample at this station, carried forward",
        "**Seasonal climatology** means: the mean of the training targets within plus or minus 1",
        "Verdicts, walk-forward, headline feature set `sensor_like` (deployable)",
        "persistence (",
        "seasonal climatology (",
    ):
        assert needle in top, needle
    order = [
        "## First lines",
        "## Warnings",
        "## Metrics",
        "## Slices",
        "## Leakage checks",
        "## Feature availability",
        "## Permutation importance",
        "## Figures",
        "## What this does and does not show",
    ]
    positions = [report.index(h) for h in order]
    assert positions == sorted(positions)
    assert "NO (diagnostic)" in report and "what a random split would have claimed" in report
    assert "improves on" not in report and "outperform" not in report
    verdict_lines = [ln for ln in top.splitlines() if ln.startswith("  - ")]
    assert verdict_lines and all(
        (" beats " in ln)
        or (" is worse than " in ln)
        or (" shows no demonstrable difference from " in ln)
        or ("cannot be compared" in ln)
        for ln in verdict_lines
    )


def test_merge_warnings_joins_identical_text_across_targets():
    merged = merge_warnings([["[event] a.", "[event] b."], ["[regression] a."]])
    assert merged == ["[event, regression] a.", "[event] b."]


def test_single_station_task_without_scorable_folds_still_reports(tmp_path):
    from tests.ml.conftest import build_long, build_task

    path = tmp_path / "small.csv"
    build_long(stations=("90001",), start="2021-01-12", step_days=80).to_csv(path, index=False)
    task = build_task(path)
    task["data"]["stations"] = ["90001"]
    res = run_task(task)
    event = res["targets"]["event"]
    assert event["walk_forward"] is None and event["folds"] == []
    assert any("NO walk-forward fold could be scored" in w for w in res["warnings"])
    report = render_report(res, [])
    assert "Walk-forward: NOT evaluated" in report and "## Leakage checks" in report


@pytest.mark.skipif(not REAL_DATA.is_file(), reason="data/clean/crp_long.csv is missing")
def test_real_pooled_chlorophyll_task_fast(tmp_path, capsys):
    assert data_path(load_task("crp_chla_pooled")) == REAL_DATA
    assert main(["--task", "crp_chla_pooled", "--fast", "--out-root", str(tmp_path)]) == 0
    out_dirs = list((tmp_path / "crp_chla_pooled").iterdir())
    assert len(out_dirs) == 1 and out_dirs[0].name.endswith("-FAST")
    report = (out_dirs[0] / "report.md").read_text(encoding="utf-8")
    lines = report.splitlines()
    assert lines[0] == "# Report: crp_chla_pooled (FAST)"
    top = report.split("## Warnings")[0]
    assert "`source=CRP` only" in top and "Stations: 13202, 15816, 17410" in top
    assert "### Primary target: event chlorophyll_a >= 10 ug/L" in top
    assert "- Walk-forward:" in top and "n_holdout =" in top
    assert "the last sample at this station, carried forward" in top
    assert "seasonal climatology beats persistence" in top or "seasonal climatology shows" in top
    assert "## Reference comparison" in report
    metrics = json.loads((out_dirs[0] / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["fast"] is True and metrics["source"] == "CRP"
    assert (out_dirs[0] / "event_by_month.png").is_file()
    assert "wrote:" in capsys.readouterr().out
