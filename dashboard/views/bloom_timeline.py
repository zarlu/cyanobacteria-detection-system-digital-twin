"""Bloom timeline: the seasonal outlook, every model at the latest sample, and a what-if."""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from dashboard.api_client import ApiClient, ApiError
from dashboard.helpers import (
    ALGO_COLOR,
    BASELINE_COLOR,
    LIKELY_COLOR,
    OUTLOOK_NOTE,
    QUIET_COLOR,
    REAL_DATA_TEXT,
    SIM_READING_TEXT,
    has_simulator_rows,
    observations_to_frame,
    outlook_table,
    ranking_frame,
    to_display_tz,
    top_model,
    version_label,
    window_text,
    winner_text,
)
from dashboard.ui import api_error, banner, empty_state, page_setup, sidebar, style_fig

page_setup(
    "Bloom timeline",
    "When a bloom is likely at each station, what every model says about the latest sample, "
    "and how the models react to a new reading.",
)
client = ApiClient.from_env()
settings = sidebar(client)
banner_slot = st.empty()

try:
    registry = client.models()
except ApiError as exc:
    api_error(exc)
    st.stop()
champions = [m for m in registry if m["status"] == "champion"]
if not champions:
    empty_state("No champion model registered yet. Run `make demo-real` (or the ML train job).")
    st.stop()

c1, c2 = st.columns([3, 2])
champion = c1.selectbox(
    "Task", champions, format_func=lambda m: m["task"], key="timeline_model",
    help="Tasks that have a registered champion model.",
)  # fmt: skip
task = champion["task"]
models = [m for m in registry if m["task"] == task]
try:
    all_preds = client.predictions(target=champion["target"])
    bench = client.benchmarks(task)
except ApiError as exc:
    api_error(exc)
    st.stop()
known = {m["version"] for m in models}
node_ids = sorted({n for n, v in zip(all_preds["node_id"], all_preds["model_version"],
                                     strict=True) if v in known})  # fmt: skip
if not node_ids:
    empty_state("No stored prediction yet. Run the ML forecast job.")
    st.stop()
try:
    outlooks = {n: client.outlook(task, n) for n in node_ids}
except ApiError as exc:
    api_error(exc)
    st.stop()
summary = outlook_table(outlooks)
soonest = summary["node"].iloc[0] if len(summary) else node_ids[0]
node_id = c2.selectbox(
    "Station (node)", node_ids, index=node_ids.index(soonest), key="timeline_node",
    help="Opens on the station whose likely bloom window comes first.",
)  # fmt: skip

try:
    outlook = outlooks[node_id]
    preds = pd.DataFrame(
        {k: v for k, v in client.predictions(node_id=node_id, target=champion["target"]).items()
         if isinstance(v, list)}
    )  # fmt: skip
    labels = observations_to_frame(
        client.observations(node_id=node_id, channel=champion["target"], kind="label")
    )
except ApiError as exc:
    api_error(exc)
    st.stop()

banner(has_simulator_rows(labels.get("source", [])), banner_slot)
if champion.get("train_sources") == "CRP":
    st.info(REAL_DATA_TEXT, icon=":material/info:")
threshold = outlook["event_threshold"]
unit = outlook.get("unit") or ""
target = outlook["target"]
event_name = f"{target} >= {threshold} {unit}"
preds["valid_at"] = pd.to_datetime(preds["valid_at"], utc=True)
ranking = ranking_frame(bench, "event")
best = top_model(ranking)
best_algo = best["algorithm"] if best else champion["algorithm"]


def series_style(version: str) -> tuple[str, dict, bool]:
    """(legend name, line style, is the top-ranked model) for a stored model_version."""
    name, algo, is_baseline = version_label(version, models)
    if is_baseline:
        dash = "dot" if "persistence" in version else "dash"
        return name, {"color": BASELINE_COLOR, "dash": dash, "width": 1.6}, False
    top = algo == best_algo
    line = {"color": ALGO_COLOR.get(algo, "#0F172A"), "width": 3.2 if top else 1.4}
    return (f"{name} (top-ranked)" if top else name), line, top


# ------------------------------------------------------------------ 1. seasonal outlook
st.subheader("When is the next bloom likely?")
head, detail = window_text(outlook)
o1, o2 = st.columns([1, 3])
o1.metric("next likely bloom window", head)
o2.markdown(detail)
o2.caption(OUTLOOK_NOTE)
months = pd.DataFrame(outlook["months"])
if months.empty:
    empty_state("No labels stored for this station, so there is no outlook.")
else:
    months["month"] = pd.to_datetime(months["month_start"])
    basis_text = {
        "station": "this station",
        "pooled_stations": "all stations pooled (too few samples here)",
        "all_months": "all samples (too few near this month)",
    }
    hover = [
        f"{m:%B %Y}<br>{e} of {n} past samples reached the event level<br>history: {basis_text[b]}"
        for m, e, n, b in zip(
            months["month"], months["n_events"], months["n_labels"], months["basis"], strict=True
        )
    ]
    fig = go.Figure(
        go.Bar(
            x=months["month"].dt.strftime("%b %Y"),
            y=months["probability"],
            marker_color=[LIKELY_COLOR if v else QUIET_COLOR for v in months["likely"]],
            text=[f"{e}/{n}" for e, n in zip(months["n_events"], months["n_labels"], strict=True)],
            textposition="outside",
            hovertext=hover,
            hoverinfo="text",
        )
    )
    fig.add_hline(
        y=outlook["likely_probability"], line_dash="dot", line_color="#475569",
        annotation_text="likely-month level (PLACEHOLDER)", annotation_position="top left",
    )  # fmt: skip
    fig.update_layout(
        yaxis={"range": [0, 1.12]},
        yaxis_title=f"share of past samples with {event_name} (0 to 1)",
        xaxis_title="calendar month (above each bar: events / past samples near that month)",
        showlegend=False,
    )
    st.plotly_chart(style_fig(fig, 340), width="stretch")
with st.expander("All stations: next likely bloom window", expanded=True):
    if summary.empty:
        empty_state("No station has an outlook yet.")
    else:
        st.dataframe(summary, hide_index=True, width="stretch")

# ------------------------------------------------------------------ 2. every model, latest sample
st.subheader("What every model says about the latest sample")
latest_at = preds["valid_at"].max()
latest = preds[preds["valid_at"] == latest_at]
age_days = (datetime.now(UTC) - latest_at.to_pydatetime()).days
shown = to_display_tz([latest_at], settings["tz"]).iloc[0]
rows = []
for _, r in latest.iterrows():
    name, style, top = series_style(r["model_version"])
    rows.append(
        {
            "predictor": name,
            f"P({event_name})": None if pd.isna(r["p_event"]) else round(r["p_event"], 2),
            f"estimate ({unit})": None if pd.isna(r["yhat"]) else round(r["yhat"], 1),
            "90% interval": f"{r['lo']:.1f} to {r['hi']:.1f}" if pd.notna(r["lo"]) else "-",
            "_top": top,
            "_baseline": r["model_version"] not in known,
        }
    )
table = pd.DataFrame(rows).sort_values(["_top", "_baseline"], ascending=[False, True])
top_row = table.iloc[0] if len(table) and table["_top"].iloc[0] else None
a, b, c = st.columns(3)
if top_row is not None:
    a.metric(f"P({event_name}), top-ranked model", f"{top_row[f'P({event_name})']:.2f}")
    b.metric(f"estimated {target} ({unit})", f"{top_row[f'estimate ({unit})']:.1f}",
             f"90% interval {top_row['90% interval']}", delta_color="off")  # fmt: skip
c.metric("sample date", f"{shown:%Y-%m-%d}", f"{age_days} days ago", delta_color="off")
st.dataframe(table.drop(columns=["_top", "_baseline"]), hide_index=True, width="stretch")
st.caption(
    "Nowcasts for the day of the sample, from that visit's water temperature, pH, discharge, "
    "air temperature, the season and the station. They are as current as the latest stored "
    "reading and do not look ahead. Logistic gives a probability only; ridge an estimate only."
)

# ------------------------------------------------------------------ 3. ranking
st.subheader("Which model ranks first?")
if ranking.empty:
    empty_state("No backtest report for this task.")
else:
    st.markdown(winner_text(ranking))
    st.dataframe(
        ranking.drop(columns=["key", "algorithm", "is_baseline"]).rename(
            columns={"walk-forward": "PR-AUC walk-forward", "final hold-out": "PR-AUC hold-out"}
        ),
        hide_index=True,
        width="stretch",
    )
    st.caption(
        "Ranked by walk-forward PR-AUC (0 to 1, higher is better) from the backtest report. "
        "Intervals and every verdict are on the Model benchmarks page."
    )

# ------------------------------------------------------------------ 4. all models over time
st.subheader("All models against the lab labels")
fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.08)
start = preds["valid_at"].min()
good = labels[(labels["quality"] == "good") & (labels["ts"] >= start)] if len(labels) else labels
versions = sorted(preds["model_version"].unique(), key=lambda v: (not series_style(v)[2], v))
for version in reversed(versions):  # top-ranked drawn last, on top
    name, line, top = series_style(version)
    g = preds[preds["model_version"] == version].sort_values("valid_at")
    if g["yhat"].notna().any():
        fig.add_trace(go.Scatter(x=g["valid_at"], y=g["yhat"], mode="lines", line=line, name=name,
                                 legendgroup=version), row=1, col=1)  # fmt: skip
    if g["p_event"].notna().any():
        fig.add_trace(go.Scatter(x=g["valid_at"], y=g["p_event"], mode="lines", line=line,
                                 name=name, legendgroup=version,
                                 showlegend=not g["yhat"].notna().any()), row=2, col=1)  # fmt: skip
if len(good):
    fig.add_trace(go.Scatter(x=good["ts"], y=good["value"], mode="markers",
                             marker={"symbol": "diamond", "size": 9, "color": "#0F172A"},
                             name="lab label"), row=1, col=1)  # fmt: skip
fig.add_hline(y=threshold, line_dash="dash", line_color=LIKELY_COLOR, row=1, col=1)
fig.update_yaxes(title_text=f"{target} ({unit})", row=1, col=1)
fig.update_yaxes(title_text=f"P({event_name}) (0 to 1)", range=[0, 1.05], row=2, col=1)
fig.update_xaxes(title_text="sample time (UTC)", row=2, col=1)
fig.update_layout(legend={"orientation": "h", "y": 1.1})
st.plotly_chart(style_fig(fig, 560), width="stretch")
st.caption(
    f"Predictions stored by the forecast job for every visit from {start:%Y-%m-%d} on, the "
    "period no model was trained on. Thick line: the top-ranked model. Grey: baselines. Red "
    'dashed line: the event level. A left-censored label ("< x") is drawn at its stored value. '
    "Click a legend entry to hide or show a line."
)

# ------------------------------------------------------------------ 5. what-if
st.subheader("Try it: simulate a sensor reading")
st.markdown(
    "Draws one random reading for this station (inside the range of its past readings) and "
    "shows how each model's bloom probability moves from the latest real sample."
)
if st.button("Simulate a reading", type="primary", key="simulate"):
    st.session_state["sim_seed"] = st.session_state.get("sim_seed", 0) + 1
    st.session_state["sim_node"] = node_id
seed = st.session_state.get("sim_seed")
if seed and st.session_state.get("sim_node") == node_id:
    try:
        sim = client.simulate(task, node_id, seed)
    except ApiError as exc:
        api_error(exc)
        st.stop()
    st.warning(SIM_READING_TEXT, icon=":material/warning:")
    left, right = st.columns([2, 3])
    readings = pd.DataFrame(sim["readings"])
    readings["drawn between"] = [f"{lo} and {hi}" for lo, hi in readings["drawn_between"]]
    left.markdown(f"**Simulated reading #{seed}**, dated {sim['date']}")
    left.dataframe(readings[["channel", "value", "unit", "drawn between"]], hide_index=True,
                   width="stretch")  # fmt: skip
    real_p = dict(zip(latest["model_version"], latest["p_event"], strict=True))
    names, sim_p, was_p, colors = [], [], [], []
    for p in sim["predictions"]:
        if p["p_event"] is None:
            continue
        name, line, _ = series_style(p["model_version"])
        names.append(name)
        sim_p.append(p["p_event"])
        was_p.append(real_p.get(p["model_version"]))
        colors.append(line["color"])
    fig = go.Figure()
    fig.add_trace(go.Bar(x=names, y=was_p, name=f"latest real sample ({shown:%Y-%m-%d})",
                         marker_color="#CBD5E1"))  # fmt: skip
    fig.add_trace(go.Bar(x=names, y=sim_p, name="simulated reading", marker_color=colors,
                         text=[f"{v:.2f}" for v in sim_p], textposition="outside"))  # fmt: skip
    fig.update_layout(barmode="group", yaxis={"range": [0, 1.12]},
                      yaxis_title=f"P({event_name}) (0 to 1)", xaxis_title="predictor",
                      legend={"orientation": "h", "y": 1.15})  # fmt: skip
    right.plotly_chart(style_fig(fig, 340), width="stretch")
    st.caption(
        "Station climatology does not use the reading: it depends only on the station and the "
        "month, so its bar moves only because the simulated reading is dated today."
    )
