"""Overview: one card per node, placeholders for alerts and the champion model."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import streamlit as st

from dashboard.api_client import ApiClient, ApiError
from dashboard.helpers import (
    KEY_CHANNELS,
    has_simulator_rows,
    latest_per_channel,
    minutes_silent_text,
    observations_to_frame,
    to_display_tz,
)
from dashboard.ui import (
    REFRESH_SECONDS,
    api_error,
    banner,
    empty_state,
    page_setup,
    quality_counts,
    role_tag,
    sidebar,
)

page_setup(
    "Overview", "Sensor nodes of the reach, their latest readings, and the registered model."
)
client = ApiClient.from_env()
settings = sidebar(client)
banner_slot = st.empty()


def render() -> None:
    """Fetch nodes and their latest values, draw the cards."""
    try:
        nodes = client.nodes()
    except ApiError as exc:
        api_error(exc)
        return
    if not nodes:
        empty_state("No nodes configured. Check config/reach.yaml.")
        return
    file_nodes = [n for n in nodes if n["role"] == "unknown" and n["n_rows"] > 0]
    nodes = [n for n in nodes if n not in file_nodes]
    simulated = False
    since = datetime.now(UTC) - timedelta(hours=24)
    cols = st.columns(min(len(nodes), 4))
    for i, node in enumerate(nodes):
        # Fixed-height rows first (title, role, last seen, counts) so they line up across cards;
        # the free-length description goes last.
        with cols[i % len(cols)], st.container(border=True, height="stretch"):
            st.subheader(node["node_id"], anchor=False)
            role_tag(node["role"])
            seen = minutes_silent_text(node["minutes_since_seen"])
            if node["last_seen"]:
                shown = to_display_tz([node["last_seen"]], settings["tz"]).iloc[0]
                seen = f"{seen} ({shown:%Y-%m-%d %H:%M %Z})"
            st.markdown(f"**Last seen:** {seen}")
            quality_counts(
                node["good_1h"], node["suspect_1h"], node["bad_1h"], "Readings in the last hour"
            )
            try:
                body = client.observations(node_id=node["node_id"], start=since, kind="measurement")
            except ApiError as exc:
                st.warning(f"latest values unavailable: {exc}")
                st.caption(node.get("name") or "")
                continue
            df = observations_to_frame(body)
            simulated = simulated or has_simulator_rows(df.get("source", []))
            latest = latest_per_channel(df)
            if not latest:
                st.caption("no measurements in the last 24 h")
            for ch in KEY_CHANNELS:
                if ch in latest:
                    value, unit, quality, _ = latest[ch]
                    flag = "" if quality == "good" else f" ({quality})"
                    st.markdown(f"`{ch}` {value:.3g} {unit}{flag}")
            st.caption(node.get("name") or "")
    banner(simulated, banner_slot)
    if file_nodes:
        st.caption(
            f"{len(file_nodes)} more nodes hold rows loaded from files (not live sensors), for "
            "example lab grab samples. They are on the Bloom timeline and Live signals pages."
        )
    st.divider()
    a, b = st.columns(2)
    with a, st.container(border=True, height="stretch"):
        st.subheader("Active alerts", anchor=False)
        empty_state("No alert engine yet (alerts phase). Rules will live in config/alerts.yaml.")
    with b, st.container(border=True, height="stretch"):
        st.subheader("Champion model", anchor=False)
        try:
            champions = [m for m in client.models() if m["status"] == "champion"]
        except ApiError:
            champions = []
        if not champions:
            empty_state("No model registered yet. Run `make demo-real` to train and register one.")
        for m in champions:
            st.markdown(f"`{m['version']}`")
            st.caption(
                f"Task {m['task']}, target {m['target']}, trained on {m['n_train']} labels up to "
                f"{str(m['train_end'])[:10]} (source {m['train_sources']}). Scores are on the "
                "Model benchmarks page."
            )


if settings["auto_refresh"]:

    @st.fragment(run_every=REFRESH_SECONDS)
    def live() -> None:
        render()

    live()
else:
    render()
