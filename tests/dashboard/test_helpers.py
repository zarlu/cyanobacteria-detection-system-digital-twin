"""Unit tests for the dashboard's pure helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from dashboard.helpers import (
    BANNER_TEXT,
    has_simulator_rows,
    latest_per_channel,
    minutes_silent_text,
    observations_to_frame,
    resample_param,
    split_by_quality,
    time_range_bounds,
    to_display_tz,
)


def test_to_display_tz_converts_for_display_only() -> None:
    s = to_display_tz(["2026-07-01T18:00:00Z", "2026-01-01T18:00:00Z"], "America/Chicago")
    assert s.iloc[0].hour == 13 and s.iloc[0].utcoffset() == timedelta(hours=-5)  # CDT
    assert s.iloc[1].hour == 12 and s.iloc[1].utcoffset() == timedelta(hours=-6)  # CST
    utc = to_display_tz(["2026-07-01T18:00:00Z"], "UTC")
    assert utc.iloc[0].hour == 18
    assert str(s.iloc[0].tz) == "America/Chicago"


def test_resample_param_mapping() -> None:
    assert resample_param("raw") is None
    assert resample_param("15 min") == "15min"
    assert resample_param("1 h") == "1h"
    assert resample_param("1 day") == "1D"
    with pytest.raises(KeyError):
        resample_param("2 h")


def test_banner_logic() -> None:
    assert has_simulator_rows(["sensor", "simulator"]) is True
    assert has_simulator_rows(["sensor", "HI713"]) is False
    assert has_simulator_rows([]) is False
    assert has_simulator_rows(pd.Series(["simulator"])) is True
    assert "SIMULATED DATA" in BANNER_TEXT and "pipeline validation only" in BANNER_TEXT


def test_time_range_bounds() -> None:
    now = datetime(2026, 10, 10, 12, tzinfo=UTC)
    start, end = time_range_bounds("last 6 h", now)
    assert end == now and start == now - timedelta(hours=6)
    start, end = time_range_bounds("last 30 d", now)
    assert start == now - timedelta(days=30)
    start, end = time_range_bounds(
        "custom", now, custom=(datetime(2026, 10, 1), datetime(2026, 10, 2))
    )
    assert start.tzinfo is UTC and start.day == 1 and end.day == 2
    with pytest.raises(ValueError):
        time_range_bounds("custom", now)


def test_quality_split_and_frame() -> None:
    body = {
        "count": 3,
        "resample": None,
        "ts": ["2026-10-01T00:00:00Z", "2026-10-01T00:15:00Z", "2026-10-01T00:30:00Z"],
        "node_id": ["n"] * 3,
        "channel": ["ph"] * 3,
        "value": [7.0, 7.1, 9.9],
        "unit": ["pH"] * 3,
        "quality": ["good", "suspect", "bad"],
        "kind": ["measurement"] * 3,
        "source": ["simulator"] * 3,
        "meta": [{}, {"reason": "ph_settling"}, {"reason": "spike"}],
    }
    df = observations_to_frame(body)
    assert str(df["ts"].dt.tz) == "UTC" and len(df) == 3
    parts = split_by_quality(df)
    assert len(parts["good"]) == len(parts["suspect"]) == len(parts["bad"]) == 1
    assert parts["bad"]["value"].iloc[0] == 9.9
    latest = latest_per_channel(df)
    assert latest["ph"][0] == 9.9 and latest["ph"][2] == "bad"
    empty = observations_to_frame({"count": 0, "ts": [], "value": [], "quality": [], "source": []})
    assert empty.empty and latest_per_channel(empty) == {}


def test_minutes_silent_text() -> None:
    assert minutes_silent_text(None) == "never seen"
    assert minutes_silent_text(5.2) == "5 min ago"
    assert minutes_silent_text(150) == "2.5 h ago"
    assert minutes_silent_text(3 * 1440) == "3.0 d ago"


# ----------------------------------------------------------------- benchmarks and outlook
def _outlook(days, n_months=2, share=0.75):
    return {
        "likely_probability": 0.5,
        "months": [{"probability": share}],
        "n_labels_station": 20,
        "last_label_date": "2026-03-30",
        "next_window": {
            "month_start": "2027-05-01",
            "last_month_start": "2027-06-01" if n_months == 2 else "2027-05-01",
            "days_until": days,
            "n_months": n_months,
            "peak_probability": share,
        },
    }


def test_window_text_states_the_lead_time_in_plain_units():
    from dashboard.helpers import window_text

    assert window_text(_outlook(0))[0] == "in a likely bloom month now"
    assert window_text(_outlook(9))[0] == "in about 9 days"
    assert window_text(_outlook(35))[0] == "in about 5 weeks"
    head, detail = window_text(_outlook(170))
    assert head == "in about 6 months" and "May 2027 to June 2027" in detail and "75%" in detail
    assert window_text({"months": [{"probability": 0.1}], "next_window": None})[0].startswith(
        "none"
    )
    assert window_text({"months": []})[0] == "no outlook"


def test_outlook_table_sorts_soonest_first_and_keeps_nodes_without_a_window():
    from dashboard.helpers import outlook_table

    table = outlook_table(
        {
            "crp-a": {"months": [{"probability": 0.1}], "next_window": None},
            "crp-b": _outlook(170),
            "crp-c": _outlook(20),
            "crp-d": {"months": []},
        }
    )
    assert table["node"].tolist() == ["crp-c", "crp-b", "crp-a"]
    assert outlook_table({}).empty


def test_benchmark_frame_puts_baselines_first_and_words_the_verdicts():
    from dashboard.helpers import BASELINE_COLOR, benchmark_frame

    rows = [
        {"key": "rf@s", "label": "rf on s", "is_baseline": False, "algorithm": "random_forest",
         "value": 0.7, "lo": 0.6, "hi": 0.8,
         "verdicts": {"station_climatology": {"verdict": "no_difference", "sentence": "x"}}},
        {"key": "persistence", "label": "persistence", "is_baseline": True,
         "algorithm": "persistence", "value": 0.3, "lo": 0.2, "hi": 0.4, "verdicts": {}},
        {"key": "skip", "label": "skip", "is_baseline": True, "algorithm": "skip",
         "value": None, "lo": None, "hi": None, "verdicts": {}},
    ]  # fmt: skip
    body = {"kinds": {"event": {"blocks": {"holdout": {"rows": rows}}}}}
    df = benchmark_frame(body, "event", "holdout")
    assert df["key"].tolist() == ["persistence", "rf@s"]
    assert df["color"].iloc[0] == BASELINE_COLOR
    assert df["vs station climatology"].iloc[1] == "no demonstrable difference from"
    assert benchmark_frame(body, "event", "walk_forward").empty


def _bench_body():
    def row(key, value, verdict=None, baseline=False):
        algo = key.split("@")[0]
        verdicts = {"station_climatology": {"verdict": verdict, "sentence": "s"}} if verdict else {}
        base = {"key": key, "label": key, "is_baseline": baseline, "algorithm": algo}
        return {**base, "value": value, "lo": value - 0.1, "hi": value + 0.1, "verdicts": verdicts}

    wf = [row("base_rate", 0.2, "worse", True), row("station_climatology", 0.684, None, True),
          row("random_forest@s", 0.683, "no_difference"), row("xgboost@s", 0.622, "no_difference"),
          row("logistic@s", 0.513, "worse")]  # fmt: skip
    hold = [row("station_climatology", 0.781, None, True), row("random_forest@s", 0.787),
            row("xgboost@s", 0.816)]  # fmt: skip
    blocks = {"walk_forward": {"rows": wf}, "holdout": {"rows": hold}}
    return {"kinds": {"event": {"higher_is_better": True, "blocks": blocks}}}


def test_ranking_orders_by_walk_forward_and_names_the_top_model_honestly():
    from dashboard.helpers import ranking_frame, top_model, winner_text

    ranking = ranking_frame(_bench_body())
    assert ranking["key"].tolist() == [
        "station_climatology", "random_forest@s", "xgboost@s", "logistic@s"
    ]  # fmt: skip
    assert ranking["rank"].tolist() == [1, 2, 3, 4]
    assert ranking["final hold-out"].tolist()[:3] == [0.781, 0.787, 0.816]
    assert top_model(ranking)["algorithm"] == "random_forest"  # baselines are not "the model"
    text = winner_text(ranking)
    assert "random forest" in text and "without being a proven winner" in text
    assert "beats" not in text
    assert ranking_frame({"kinds": {}}).empty
    assert winner_text(ranking_frame({"kinds": {}})) == "No trained model has been scored yet."


def test_version_label_separates_baselines_from_models():
    from dashboard.helpers import version_label

    models = [{"version": "t-random_forest-1", "algorithm": "random_forest"}]
    assert version_label("persistence-v1", models) == ("persistence", "persistence-v1", True)
    assert version_label("t-random_forest-1", models) == ("random forest", "random_forest", False)
