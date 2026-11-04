"""Shared Streamlit pieces: sidebar, API status, banner, error states. No business logic."""

from __future__ import annotations

import contextlib
import os
from datetime import UTC, datetime, time, timedelta
from typing import Any

import streamlit as st

from dashboard.api_client import ApiClient, ApiError
from dashboard.helpers import BANNER_TEXT, DEFAULT_TZ, TIME_RANGES, TZ_OPTIONS, time_range_bounds

REFRESH_SECONDS = 15


def page_setup(title: str) -> None:
    """Page config shared by every view (safe to call in each standalone view)."""
    with contextlib.suppress(st.errors.StreamlitAPIException):  # already set by app.py
        st.set_page_config(
            page_title=f"{title} - Cyanobacteria Detection System Digital Twin",
            layout="wide",
        )
    st.title(title)


def sidebar(client: ApiClient) -> dict[str, Any]:
    """Render the sidebar and return the current settings (tz, range, refresh)."""
    with st.sidebar:
        st.markdown("### Cyanobacteria Detection System Digital Twin")
        status = st.empty()
        try:
            h = client.health()
            status.success(f"API ok - v{h['version']} - {h['utc_now'][:19]}Z")
        except ApiError as exc:
            status.error(f"API down: {exc}")
        st.caption(f"API: {client.base_url}")
        default_tz = os.environ.get("TZ_DISPLAY", DEFAULT_TZ)
        tz_index = TZ_OPTIONS.index(default_tz) if default_tz in TZ_OPTIONS else 0
        tz = st.radio("Display time zone", TZ_OPTIONS, index=tz_index, horizontal=True, key="tz")
        st.caption("Data is stored and exchanged in UTC; only the display changes.")
        choice = st.selectbox("Time range", list(TIME_RANGES), index=3, key="time_range")
        custom = None
        if choice == "custom":
            today = datetime.now(UTC).date()
            d0 = st.date_input("start (UTC)", today - timedelta(days=7), key="custom_start")
            d1 = st.date_input("end (UTC)", today, key="custom_end")
            custom = (
                datetime.combine(d0, time.min, tzinfo=UTC),
                datetime.combine(d1, time.max, tzinfo=UTC),
            )
        start, end = time_range_bounds(choice, custom=custom)
        auto = st.toggle(f"Auto-refresh every {REFRESH_SECONDS} s", value=True, key="auto_refresh")
    return {"tz": tz or DEFAULT_TZ, "start": start, "end": end, "auto_refresh": auto}


def banner(show: bool, slot: Any | None = None) -> None:
    """Persistent banner whenever displayed rows include simulated data."""
    target = slot or st
    if show:
        target.warning(BANNER_TEXT, icon="⚠️")


def api_error(exc: ApiError) -> None:
    """Friendly error state (never a traceback)."""
    st.error(f"Could not load data from the API. {exc}")
    st.info("Start it with `make api` (or `make demo`) and check `API_URL` in `.env`.")


def empty_state(text: str) -> None:
    """Friendly empty state."""
    st.info(text)
