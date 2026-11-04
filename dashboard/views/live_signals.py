"""Live signals: time series per node and channel with quality encoding and labels."""

from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from dashboard.api_client import ApiClient, ApiError
from dashboard.helpers import (
    QUALITY_STYLE,
    RESAMPLE_OPTIONS,
    has_simulator_rows,
    observations_to_frame,
    resample_param,
    split_by_quality,
    to_display_tz,
)
from dashboard.ui import REFRESH_SECONDS, api_error, banner, empty_state, page_setup, sidebar

page_setup("Live signals")
client = ApiClient.from_env()
settings = sidebar(client)
banner_slot = st.empty()

try:
    all_nodes = [n["node_id"] for n in client.nodes()]
    channels = {c["channel"]: c for c in client.channels()}
except ApiError as exc:
    api_error(exc)
    st.stop()

measurement_channels = [c for c, s in channels.items() if s["default_kind"] == "measurement"]
external_channels = [c for c, s in channels.items() if s["default_kind"] == "external"]
label_channels = [c for c, s in channels.items() if s["default_kind"] == "label"]
c1, c2, c3, c4 = st.columns([2, 3, 1, 1])
nodes = c1.multiselect("Nodes", all_nodes, default=all_nodes[:1], key="nodes")
default_channels = [c for c in ("water_temp", "ph") if c in measurement_channels]
picked = c2.multiselect(
    "Channels",
    measurement_channels + external_channels + label_channels,
    default=default_channels,
    key="channels",
    help="Labels (grab samples) are shown as diamonds for the picked channels.",
)
resample_label = c3.selectbox("Resample", list(RESAMPLE_OPTIONS), index=0, key="resample")
secondary = c4.toggle("External on 2nd axis", value=True, key="secondary")
resample = resample_param(resample_label)


def fetch() -> tuple[list, list]:
    frames, label_frames = [], []
    for node in nodes:
        for ch in picked:
            kind = channels[ch]["default_kind"]
            if kind != "label":
                body = client.observations(
                    node_id=node,
                    channel=ch,
                    kind=kind,
                    start=settings["start"],
                    end=settings["end"],
                    resample=resample,
                    agg="mean" if resample else None,
                )
                frames.append(observations_to_frame(body))
            labels = client.observations(
                node_id=node, channel=ch, kind="label", start=settings["start"], end=settings["end"]
            )
            label_frames.append(observations_to_frame(labels))
    return frames, label_frames


def render() -> None:
    if not nodes or not picked:
        empty_state("Pick at least one node and one channel.")
        return
    try:
        frames, label_frames = fetch()
    except ApiError as exc:
        api_error(exc)
        return
    import pandas as pd

    empty = observations_to_frame({"count": 0})
    non_empty = [f for f in frames if not f.empty]
    df = pd.concat(non_empty, ignore_index=True) if non_empty else empty
    non_empty_labels = [f for f in label_frames if not f.empty]
    labels = pd.concat(non_empty_labels, ignore_index=True) if non_empty_labels else empty
    banner(
        has_simulator_rows(list(df.get("source", [])) + list(labels.get("source", []))), banner_slot
    )
    if df.empty and labels.empty:
        empty_state("No rows in this time range for the selected nodes and channels.")
        return
    fig = go.Figure()
    units = sorted({channels[c]["unit"] for c in picked if c not in external_channels})
    ext_units = sorted({channels[c]["unit"] for c in picked if c in external_channels})
    for (node, ch), g in df.groupby(["node_id", "channel"], sort=True):
        on_secondary = secondary and ch in external_channels
        shown = g.assign(ts_disp=to_display_tz(g["ts"], settings["tz"]))
        unit = channels[ch]["unit"]
        parts = split_by_quality(shown)
        hover = "%{y:.3g} " + unit + "<br>%{x}"
        if resample and "n" in shown:
            hover += "<br>n=%{customdata}"
        for q, part in parts.items():
            if part.empty:
                continue
            style = QUALITY_STYLE[q]
            fig.add_trace(
                go.Scatter(
                    x=part["ts_disp"],
                    y=part["value"],
                    name=f"{node} {ch}" + ("" if q == "good" else f" ({q})"),
                    customdata=part.get("n"),
                    hovertemplate=hover,
                    yaxis="y2" if on_secondary else "y",
                    legendgroup=f"{node}-{ch}",
                    **style,
                )
            )
    for (node, ch), g in labels.groupby(["node_id", "channel"], sort=True):
        shown = g.assign(ts_disp=to_display_tz(g["ts"], settings["tz"]))
        text = [
            f"{ch} {v:.3g} {u} - {s} - {m.get('sample_id', '')} {m.get('emulates', '')}"
            for v, u, s, m in zip(g["value"], g["unit"], g["source"], g["meta"], strict=True)
        ]
        fig.add_trace(
            go.Scatter(
                x=shown["ts_disp"],
                y=shown["value"],
                mode="markers",
                marker={"symbol": "diamond", "size": 10},
                name=f"{node} {ch} (label)",
                text=text,
                hovertemplate="%{text}<br>%{x}",
                yaxis="y",
            )
        )
    fig.update_layout(
        height=520,
        xaxis_title=f"time ({settings['tz']})",
        yaxis_title=", ".join(units) if units else "value",
        yaxis2={"title": ", ".join(ext_units) or "external", "overlaying": "y", "side": "right"}
        if secondary and ext_units
        else None,
        legend={"orientation": "h", "y": -0.2},
        margin={"l": 40, "r": 40, "t": 20, "b": 40},
    )
    st.plotly_chart(fig, width="stretch")
    st.caption(
        "good = line, suspect = hollow orange markers, bad = red x (excluded from the line), "
        "labels = diamonds. Units from /channels."
    )
    table = pd.concat([df, labels], ignore_index=True)
    table = table.assign(
        **{f"ts ({settings['tz']})": to_display_tz(table["ts"], settings["tz"]).astype(str)}
    )
    show_cols = [
        f"ts ({settings['tz']})",
        "ts",
        "node_id",
        "channel",
        "value",
        "unit",
        "quality",
        "kind",
        "source",
    ]
    if "n" in table:
        show_cols.append("n")
    st.dataframe(
        table[show_cols].sort_values("ts", ascending=False), width="stretch", hide_index=True
    )
    st.download_button(
        "Download shown data as CSV",
        table[show_cols].to_csv(index=False).encode(),
        file_name="observations.csv",
        mime="text/csv",
    )


if settings["auto_refresh"]:

    @st.fragment(run_every=REFRESH_SECONDS)
    def live() -> None:
        render()

    live()
else:
    render()
