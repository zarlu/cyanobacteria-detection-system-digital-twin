"""Headless smoke tests of every view with streamlit.testing.v1.AppTest."""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from dashboard.helpers import BANNER_TEXT

VIEWS = Path(__file__).resolve().parents[2] / "dashboard" / "views"
PAGES = sorted(p.name for p in VIEWS.glob("*.py"))


def _run(page: str, api_url: str, monkeypatch: pytest.MonkeyPatch, timeout: float = 60) -> AppTest:
    monkeypatch.setenv("API_URL", api_url)
    at = AppTest.from_file(str(VIEWS / page), default_timeout=timeout)
    at.run()
    return at


def _texts(at: AppTest) -> str:
    chunks = (
        [w.value for w in at.warning] + [w.value for w in at.info] + [w.value for w in at.error]
    )
    chunks += [m.value for m in at.markdown] + [c.value for c in at.caption]
    return "\n".join(str(c) for c in chunks)


def test_pages_exist() -> None:
    assert PAGES == ["live_signals.py", "overview.py"]


@pytest.mark.parametrize("page", PAGES)
def test_page_runs_on_empty_database_without_exception(page: str, empty_api, monkeypatch) -> None:
    at = _run(page, empty_api.url, monkeypatch)
    assert not at.exception, [e.value for e in at.exception]
    text = _texts(at)
    assert BANNER_TEXT not in text
    if page == "overview.py":
        assert "no measurements in the last 24 h" in text
        assert "No alert engine yet" in text and "No model registered yet" in text
    else:
        assert "No rows in this time range" in text


@pytest.mark.parametrize("page", PAGES)
def test_page_shows_banner_with_simulated_rows(page: str, populated_api, monkeypatch) -> None:
    at = _run(page, populated_api.url, monkeypatch)
    assert not at.exception, [e.value for e in at.exception]
    assert BANNER_TEXT in _texts(at)


@pytest.mark.parametrize("page", PAGES)
def test_page_handles_api_down(page: str, monkeypatch) -> None:
    at = _run(page, "http://127.0.0.1:9", monkeypatch)  # nothing listens on port 9
    assert not at.exception, [e.value for e in at.exception]
    assert "Could not load data from the API" in _texts(at) or "API down" in _texts(at)


def test_live_signals_resample_and_utc_toggle(populated_api, monkeypatch) -> None:
    at = _run("live_signals.py", populated_api.url, monkeypatch)
    at.selectbox(key="resample").select("1 h").run()
    assert not at.exception, [e.value for e in at.exception]
    at.radio(key="tz").set_value("UTC").run()
    assert not at.exception
    charts = at.get("plotly_chart")
    assert charts, "no chart rendered"
    assert "time (UTC)" in str(charts[0].proto)
    assert at.dataframe, "data table missing"
    assert at.sidebar.toggle(key="auto_refresh").value is True
    at.sidebar.toggle(key="auto_refresh").set_value(False).run()
    assert not at.exception


def test_overview_cards_list_every_node(populated_api, monkeypatch) -> None:
    at = _run("overview.py", populated_api.url, monkeypatch)
    heads = {h.value for h in at.subheader}
    assert {"node-intake", "node-downstream", "node-bench", "effluent-zacate"} <= heads
