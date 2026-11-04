"""Dashboard entry point: ``streamlit run dashboard/app.py`` (or ``make dash``)."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

VIEWS = Path(__file__).parent / "views"

st.set_page_config(page_title="Cyanobacteria Detection System Digital Twin", layout="wide")
page = st.navigation(
    [
        st.Page(str(VIEWS / "overview.py"), title="Overview", icon="🏠", default=True),
        st.Page(str(VIEWS / "live_signals.py"), title="Live signals", icon="📈"),
    ]
)
page.run()
