"""Repository rules for the ML harness: no random splitters, config over code, task specs."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

from ml.config import REPO, TASK_DIR, TaskError, deep_merge, list_tasks, load_raw, load_task

ML_DIR = REPO / "ml"
FORBIDDEN = re.compile(r"train_test_split|KFold|ShuffleSplit|shuffle\s*=\s*True")
ALLOWED_FILE = ML_DIR / "drills.py"
ALLOWED_FUNCTION = "invalid_random_split_demo"


def _allowed_lines() -> set[int]:
    tree = ast.parse(ALLOWED_FILE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == ALLOWED_FUNCTION:
            return set(range(node.lineno, node.end_lineno + 1))
    raise AssertionError(f"{ALLOWED_FUNCTION} not found in {ALLOWED_FILE}")


def forbidden_hits(root: Path = ML_DIR) -> list[str]:
    allowed = _allowed_lines()
    hits = []
    files = sorted([*root.rglob("*.py"), *root.rglob("*.yaml")])
    for path in files:
        if "__pycache__" in path.parts:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if FORBIDDEN.search(line) and not (path == ALLOWED_FILE and number in allowed):
                hits.append(f"{path.relative_to(REPO)}:{number}: {line.strip()}")
    return hits


def test_no_random_splitter_outside_the_one_demo_function():
    assert forbidden_hits() == []
    demo_source = "\n".join(
        line
        for n, line in enumerate(ALLOWED_FILE.read_text(encoding="utf-8").splitlines(), start=1)
        if n in _allowed_lines()
    )
    assert "KFold" in demo_source  # the demo really is the one place that has it


def test_the_grep_catches_a_planted_splitter(tmp_path):
    for bad in (
        "from sklearn.model_selection import train_test_split",
        "cv = KFold(5)",
        "ShuffleSplit(n_splits=3)",
        "folds = split(x, shuffle=True)",
        "folds = split(x, shuffle = True)",
    ):
        (tmp_path / "bad.py").write_text(f"{bad}\n", encoding="utf-8")
        assert len(_scan(tmp_path)) == 1, bad


def _scan(root: Path) -> list[str]:
    return [
        line
        for path in root.rglob("*.py")
        for line in path.read_text(encoding="utf-8").splitlines()
        if FORBIDDEN.search(line)
    ]


def test_no_station_or_channel_names_hard_coded_in_python():
    stations = set()
    for name in list_tasks():
        stations.update(str(s) for s in load_raw(name)["data"]["stations"])
    assert stations  # read from the YAML, never listed here
    pattern = re.compile("|".join(sorted(stations)) + "|chlorophyll|phosph|crp_tss|conductance")
    for path in sorted(ML_DIR.glob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            assert not pattern.search(line), f"{path.name}:{number}: {line.strip()}"


EXPECTED = {
    "crp_chla_pooled": (["13202", "15816", "17410"], ["event", "regression"], "2014-01-01"),
    "crp_chla_13202": (["13202"], ["event", "regression"], "2014-01-01"),
    "crp_chla_15816": (["15816"], ["event", "regression"], "2014-01-01"),
    "crp_chla_17410": (["17410"], ["event", "regression"], "2014-01-01"),
    "crp_chla_pooled_allyears": (
        ["13202", "15816", "17410"],
        ["event", "regression"],
        "1995-01-01",
    ),
    "crp_tp_pooled": (["13202", "15816", "17410"], ["regression"], "2014-01-01"),
    "crp_tp_13202": (["13202"], ["regression"], "2014-01-01"),
}


def test_the_task_list_is_exactly_the_part_b_tasks():
    assert sorted(list_tasks()) == sorted(EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_task_specs_follow_the_brief(name):
    stations, targets, start = EXPECTED[name]
    task = load_task(name)
    assert task["data"]["stations"] == stations and task["targets"] == targets
    assert task["start"] == start and task["source"] == "crp_clean"
    assert task["data"]["source_filter"] == "CRP"
    assert task["split"] == {
        "first_test_year": 2018,
        "gap_days": 14,
        "min_train_labels": 20,
        "min_train_positives": 3,
        "final_holdout_frac": 0.20,
    }
    assert task["eval"]["n_boot"] == 1000 and task["eval"]["costs"] == {"c_fp": 1, "c_fn": 9}
    assert task["headline_feature_set"] == "sensor_like"
    assert task["fast_mode"] is False and "fast" not in task
    rf = {"n_estimators": 300, "min_samples_leaf": 3}
    lgbm = {"num_leaves": 7, "min_child_samples": 5, "n_estimators": 200, "learning_rate": 0.05}
    assert task["seed"] == 7
    if "event" in targets:
        assert task["target"]["event_threshold"] == 10
        assert task["models"]["event"] == {
            "logistic": {"C": 0.5},
            "random_forest": rf,
            "lightgbm": lgbm,
        }
        assert task["use_feature_sets"] == ["sensor_like", "sensor_like_prev", "lab_same_visit"]
        assert task["baselines"]["event"] == ["persistence", "seasonal_climatology", "base_rate"]
    else:
        assert "event_threshold" not in task["target"]
        assert task["use_feature_sets"] == [
            "sensor_like",
            "sensor_like_prev",
            "sensor_like_plus_tss",
        ]
    assert task["models"]["regression"] == {
        "ridge": {"alpha": 10},
        "random_forest": rf,
        "lightgbm": lgbm,
    }
    assert task["feature_sets"]["sensor_like"]["deployable"] is True
    assert task["feature_sets"]["sensor_like_prev"]["deployable"] is True
    assert task["feature_sets"]["lab_same_visit"]["deployable"] is False
    assert task["feature_sets"]["sensor_like_plus_tss"]["deployable"] is False


def test_fast_mode_only_shrinks_the_run():
    slow, fast = load_task("crp_chla_pooled"), load_task("crp_chla_pooled", fast=True)
    assert fast["fast_mode"] is True and fast["eval"]["n_boot"] == 200
    assert fast["models"]["event"]["random_forest"]["n_estimators"] < 300
    assert fast["models"]["event"]["random_forest"]["min_samples_leaf"] == 3  # merged, not replaced
    assert fast["split"] == slow["split"] and fast["target"] == slow["target"]


def test_the_abstract_base_and_broken_specs_are_rejected(tmp_path):
    with pytest.raises(TaskError, match="abstract"):
        load_task("_crp_base")
    with pytest.raises(TaskError, match="not found"):
        load_task("no_such_task")
    (tmp_path / "a.yaml").write_text("extends: b\n", encoding="utf-8")
    (tmp_path / "b.yaml").write_text("extends: a\n", encoding="utf-8")
    with pytest.raises(TaskError, match="circular"):
        load_task("a", tmp_path)
    spec = yaml.safe_load((TASK_DIR / "crp_chla_pooled.yaml").read_text(encoding="utf-8"))
    spec["extends"] = "_crp_base"
    spec["target"].pop("event_threshold")
    (tmp_path / "_crp_base.yaml").write_text(
        (TASK_DIR / "_crp_base.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "noevent.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")
    with pytest.raises(TaskError, match="event_threshold"):
        load_task("noevent", tmp_path)


def test_deep_merge_merges_dicts_and_replaces_lists():
    base = {"a": {"x": 1, "y": [1, 2]}, "b": 1}
    over = {"a": {"y": [3]}, "c": None}
    assert deep_merge(base, over) == {"a": {"x": 1, "y": [3]}, "b": 1, "c": None}
    assert base == {"a": {"x": 1, "y": [1, 2]}, "b": 1}  # inputs untouched
