"""Task specs: load `ml/tasks/<name>.yaml`, resolve `extends`, validate.

A task YAML defines the whole modeling problem (CLAUDE.md section 8). The runner holds no
task-specific logic: stations, channels, feature sets, model settings and thresholds all come
from the YAML (rule 11).
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]
TASK_DIR = REPO / "ml" / "tasks"
KINDS = ("event", "regression")


class TaskError(Exception):
    """A task spec is missing something or is inconsistent."""


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """Return base updated by over. Dicts merge recursively; anything else is replaced."""
    out = copy.deepcopy(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _read(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise TaskError(f"task file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TaskError(f"{path}: a task file must be a mapping")
    return data


def load_raw(name: str, task_dir: Path = TASK_DIR, _seen: tuple[str, ...] = ()) -> dict[str, Any]:
    """Read one task file and merge it over the file named by its `extends` key."""
    if name in _seen:
        raise TaskError(f"circular extends: {' -> '.join((*_seen, name))}")
    raw = _read(task_dir / f"{name}.yaml")
    parent = raw.pop("extends", None)
    if parent:
        base = load_raw(str(parent), task_dir, (*_seen, name))
        base.pop("abstract", None)  # a child of an abstract base is runnable
        raw = deep_merge(base, raw)
    return raw


def load_task(name: str, task_dir: Path = TASK_DIR, fast: bool = False) -> dict[str, Any]:
    """Load, resolve and validate a runnable task. `fast` merges the task's `fast:` block."""
    task = load_raw(name, task_dir)
    task["name"] = name
    return prepare_task(task, fast=fast)


def prepare_task(task: dict[str, Any], fast: bool = False) -> dict[str, Any]:
    task = copy.deepcopy(task)
    overrides = task.pop("fast", None) or {}
    if fast:
        task = deep_merge(task, overrides)
    task["fast_mode"] = bool(fast)
    validate_task(task)
    return task


def resolve_feature_set(task: dict[str, Any], name: str) -> tuple[list[dict[str, Any]], bool]:
    """Feature specs of one set (its `extends` chain first) and its deployable flag."""
    library = task["feature_sets"]
    chain: list[str] = []
    cur: str | None = name
    while cur:
        if cur not in library:
            raise TaskError(f"feature set {cur!r} is not defined in the task")
        if cur in chain:
            raise TaskError(f"circular feature-set extends at {cur!r}")
        chain.append(cur)
        cur = library[cur].get("extends")
    specs: list[dict[str, Any]] = []
    for set_name in reversed(chain):
        specs.extend(library[set_name].get("features", []))
    names = [s["name"] for s in specs]
    if len(set(names)) != len(names):
        raise TaskError(f"feature set {name!r} has duplicate feature names")
    if "deployable" not in library[name]:
        raise TaskError(f"feature set {name!r} needs a deployable: true|false flag")
    return specs, bool(library[name]["deployable"])


def validate_task(task: dict[str, Any]) -> None:
    if task.get("abstract"):
        raise TaskError(f"task {task.get('name')!r} is abstract (a base for other tasks)")
    for key in (
        "source",
        "data",
        "start",
        "target",
        "targets",
        "split",
        "feature_sets",
        "use_feature_sets",
        "headline_feature_set",
        "baselines",
        "models",
        "eval",
        "drills",
        "seed",
    ):
        if key not in task:
            raise TaskError(f"task {task.get('name')!r}: missing key {key!r}")
    if not task["data"].get("stations"):
        raise TaskError("data.stations must list at least one station")
    for kind in task["targets"]:
        if kind not in KINDS:
            raise TaskError(f"unknown target kind {kind!r}")
        if kind not in task["models"]:
            raise TaskError(f"models.{kind} is missing")
        if kind not in task["baselines"]:
            raise TaskError(f"baselines.{kind} is missing")
    if "event" in task["targets"] and task["target"].get("event_threshold") is None:
        raise TaskError("an event target needs target.event_threshold")
    if task["headline_feature_set"] not in task["use_feature_sets"]:
        raise TaskError("headline_feature_set must be one of use_feature_sets")
    for name in task["use_feature_sets"]:
        resolve_feature_set(task, name)


def list_tasks(task_dir: Path = TASK_DIR) -> list[str]:
    """Names of runnable (non-abstract) tasks."""
    names = []
    for path in sorted(task_dir.glob("*.yaml")):
        if not _read(path).get("abstract"):
            names.append(path.stem)
    return names
