"""Shared Streamlit pieces: sidebar, API status, banner, error states. No business logic."""

from __future__ import annotations

import contextlib
import html
import os
from datetime import UTC, datetime, time, timedelta
from typing import Any

import streamlit as st

from dashboard.api_client import ApiClient, ApiError
from dashboard.helpers import BANNER_TEXT, DEFAULT_TZ, TIME_RANGES, TZ_OPTIONS, time_range_bounds

REFRESH_SECONDS = 15


INK = "#0F172A"
INK_MUTED = "#475569"
BORDER = "#E2E8F0"
SURFACE = "#FFFFFF"
FONT = "'Fira Sans', -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"

STYLE = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Fira+Code:wght@500;600&family=Fira+Sans:wght@400;500;600;700&display=swap');
html, body, .stApp, p, li, label, h1, h2, h3, h4, input, textarea, th, td,
.stMarkdown, .stButton > button {{ font-family: {FONT}; }}
[data-testid="stIconMaterial"] {{ font-family: "Material Symbols Rounded" !important; }}
.block-container {{ padding-top: 2.2rem; max-width: 1280px; }}
h1 {{ font-weight: 700; letter-spacing: -0.02em; font-size: 2rem; padding-bottom: 0.1rem; }}
h2, h3 {{ font-weight: 600; letter-spacing: -0.01em; }}
h3 {{ font-size: 1.15rem; margin-top: 0.6rem; }}
[data-testid="stMetric"] {{
  background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 8px; padding: 14px 16px;
}}
[data-testid="stMetricLabel"] p {{ color: {INK_MUTED}; font-size: 0.82rem; font-weight: 500; }}
[data-testid="stMetricValue"] {{
  font-family: 'Fira Code', ui-monospace, monospace; font-weight: 600; font-size: 1.55rem;
  font-variant-numeric: tabular-nums; color: {INK};
}}
[data-testid="stSidebar"] {{ border-right: 1px solid {BORDER}; }}
[data-testid="stVerticalBlockBorderWrapper"] {{ border-radius: 8px; background: {SURFACE}; }}
[data-testid="stAlert"] {{ border-radius: 8px; }}
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p {{
  color: {INK_MUTED}; opacity: 1;
}}
.role-tag {{
  display: inline-block; padding: 2px 8px; border-radius: 999px; background: #EEF2F6;
  color: {INK_MUTED}; font-size: 0.72rem; font-weight: 600; letter-spacing: 0.04em;
  text-transform: uppercase;
}}
p.q-title {{
  color: {INK_MUTED}; font-size: 0.8rem !important; font-weight: 500; margin: 0 0 6px 0;
}}
.q-row {{
  display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin-bottom: 10px;
}}
.q-cell {{
  border: 1px solid {BORDER}; border-radius: 8px; padding: 8px 10px; background: #F8FAFC;
}}
.q-num {{
  font-family: 'Fira Code', ui-monospace, monospace; font-weight: 600; font-size: 1.25rem;
  font-variant-numeric: tabular-nums; color: {INK}; line-height: 1.2;
}}
.q-label {{
  display: flex; align-items: center; gap: 5px; color: {INK_MUTED}; font-size: 0.78rem;
  white-space: nowrap;
}}
.q-dot {{ width: 8px; height: 8px; border-radius: 50%; flex: none; }}
.stButton > button {{ border-radius: 8px; font-weight: 600; transition: background 200ms; }}
.page-lead {{ color: {INK_MUTED}; font-size: 1rem; margin: -0.4rem 0 1.1rem 0; max-width: 70ch; }}
@media (prefers-reduced-motion: reduce) {{ * {{ transition: none !important; }} }}
</style>
"""


def page_setup(title: str, lead: str | None = None) -> None:
    """Page config and shared styling for every view (safe to call in each standalone view)."""
    with contextlib.suppress(st.errors.StreamlitAPIException):  # already set by app.py
        st.set_page_config(
            page_title=f"{title} - Cyanobacteria Detection System Digital Twin",
            layout="wide",
        )
    st.markdown(STYLE, unsafe_allow_html=True)
    st.title(title)
    if lead:
        st.markdown(f'<p class="page-lead">{lead}</p>', unsafe_allow_html=True)


def style_fig(fig: Any, height: int | None = None) -> Any:
    """One look for every Plotly chart: same font, white plot area, light grid."""
    fig.update_layout(
        font={"family": FONT, "color": INK, "size": 13},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor=SURFACE,
        margin={"l": 10, "r": 16, "t": 36, "b": 10},
        hoverlabel={"font": {"family": FONT}},
    )
    fig.update_xaxes(gridcolor=BORDER, linecolor=BORDER, zeroline=False,
                     title_font={"color": INK_MUTED, "size": 12})  # fmt: skip
    fig.update_yaxes(gridcolor=BORDER, linecolor=BORDER, zeroline=False,
                     title_font={"color": INK_MUTED, "size": 12})  # fmt: skip
    if height:
        fig.update_layout(height=height)
    return fig


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
        target.warning(BANNER_TEXT, icon=":material/warning:")


QUALITY_DOT = {"good": "#16A34A", "suspect": "#D97706", "bad": "#DC2626"}


def role_tag(role: str) -> None:
    """Small pill with a node's role."""
    st.markdown(
        f'<span class="role-tag">{html.escape(role.replace("_", " "))}</span>',
        unsafe_allow_html=True,
    )


def quality_counts(good: int, suspect: int, bad: int, title: str) -> None:
    """Three equal cells with the good, suspect and bad counts; labels never truncate."""
    cells = "".join(
        f'<div class="q-cell"><div class="q-num">{int(n)}</div><div class="q-label">'
        f'<span class="q-dot" style="background:{QUALITY_DOT[name]}"></span>{name}</div></div>'
        for name, n in (("good", good), ("suspect", suspect), ("bad", bad))
    )
    st.markdown(
        f'<p class="q-title">{html.escape(title)}</p><div class="q-row">{cells}</div>',
        unsafe_allow_html=True,
    )


def api_error(exc: ApiError) -> None:
    """Friendly error state (never a traceback)."""
    st.error(f"Could not load data from the API. {exc}")
    st.info("Start it with `make api` (or `make demo`) and check `API_URL` in `.env`.")


def empty_state(text: str) -> None:
    """Friendly empty state."""
    st.info(text)
