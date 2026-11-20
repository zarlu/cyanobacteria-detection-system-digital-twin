"""Model benchmarks: every baseline and model of a task's backtest report, side by side."""

from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from dashboard.api_client import ApiClient, ApiError
from dashboard.helpers import BLOCK_TITLE, METRIC_TITLE, REAL_DATA_TEXT, benchmark_frame
from dashboard.ui import api_error, empty_state, page_setup, sidebar, style_fig

page_setup(
    "Model benchmarks",
    "The backtest scores of every model next to its baselines. Each number comes from a saved "
    "report; a model only counts as better when the data can show it.",
)
client = ApiClient.from_env()
sidebar(client)

try:
    tasks = client.benchmark_tasks()
except ApiError as exc:
    api_error(exc)
    st.stop()
if not tasks:
    empty_state("No backtest report yet. Run `python -m ml.run --task <name>`.")
    st.stop()

try:
    champions = {m["task"]: m for m in client.models() if m["status"] == "champion"}
except ApiError:
    champions = {}
default = next((t for t in tasks if t in champions), tasks[0])
c1, c2, c3 = st.columns([3, 2, 2])
task = c1.selectbox("Task", tasks, index=tasks.index(default), key="bench_task")
try:
    body = client.benchmarks(task)
except ApiError as exc:
    api_error(exc)
    st.stop()

kinds = list(body["kinds"])
kind_names = {"event": "bloom event (yes / no)", "regression": "concentration"}
kind = c2.radio("Target", kinds, format_func=lambda k: kind_names.get(k, k), key="bench_kind")
blocks = list(body["kinds"][kind]["blocks"])
if not blocks:
    empty_state("This report has no evaluated block for this target.")
    st.stop()
block = c3.radio("Evaluation", blocks, format_func=lambda b: BLOCK_TITLE.get(b, b), key="bench_bl")

if body.get("source") == "CRP":
    st.info(REAL_DATA_TEXT, icon=":material/info:")
target = body["target"]
info = body["kinds"][kind]
data = info["blocks"][block]
metric = info["metric"]
unit = target.get("unit", "")
st.caption(body.get("description") or "")
m1, m2, m3, m4 = st.columns(4)
m1.metric("test labels", data["n"])
m2.metric("distinct sample dates", data["n_dates"])
if kind == "event":
    m3.metric(f"events (>= {target['event_threshold']} {unit})", data["positives"])
m4.metric("report", body["report_date"])
if task in champions:
    st.success(f"Champion in the registry: `{champions[task]['version']}`")

df = benchmark_frame(body, kind, block)
if df.empty:
    st.warning(
        f"Nothing can be scored here: this evaluation has {data['n']} test labels"
        + (f" and {data['positives']} events" if kind == "event" else "")
        + ", so the metric is undefined. Pick the other evaluation or another task.",
        icon=":material/warning:",
    )
    st.stop()

title = METRIC_TITLE[metric] if kind == "event" else f"MAE ({unit}; lower is better)"
ci = f"{100 * (body.get('ci') or 0.9):.0f}%"
fig = go.Figure(
    go.Bar(
        x=df["value"],
        y=df["predictor"],
        orientation="h",
        marker_color=df["color"],
        error_x={
            "type": "data",
            "symmetric": False,
            "array": (df["hi"] - df["value"]).fillna(0),
            "arrayminus": (df["value"] - df["lo"]).fillna(0),
            "color": "#52514e",
            "thickness": 1.2,
        },
        text=[f"{v:.3f}" for v in df["value"]],
        textposition="outside",
        hovertemplate="%{y}<br>%{x:.3f}<extra></extra>",
    )
)
if kind == "event" and data.get("base_rate") is not None:
    fig.add_vline(x=data["base_rate"], line_dash="dot", line_color="#52514e",
                  annotation_text="base rate", annotation_position="top")  # fmt: skip
fig.update_layout(
    xaxis_title=f"{title}, {BLOCK_TITLE[block]}; bars: {ci} bootstrap interval",
    yaxis_title="predictor (grey = baseline)",
    yaxis={"autorange": "reversed"},
    showlegend=False,
)
st.plotly_chart(style_fig(fig, 80 + 44 * len(df)), width="stretch")

verdict_cols = [c for c in df.columns if c.startswith("vs ")]
table = df[["predictor", "value", "lo", "hi", *verdict_cols]].rename(
    columns={"value": metric, "lo": f"{ci} low", "hi": f"{ci} high"}
)
st.dataframe(table, hide_index=True, width="stretch")
st.caption(
    'A verdict says "beats" or "is worse than" only when the bootstrap interval of the paired '
    'difference excludes zero; otherwise it is "no demonstrable difference".'
)

with st.expander("What each baseline means"):
    for name, text in info.get("baseline_meaning", {}).items():
        st.markdown(f"- **{name.replace('_', ' ')}**: {text}")
with st.expander("Verdict sentences from the report"):
    rows = body["kinds"][kind]["blocks"][block]["rows"]
    for r in rows:
        for v in r.get("verdicts", {}).values():
            st.markdown(f"- {v['sentence']}")
if body.get("warnings"):
    with st.expander(f"Report warnings ({len(body['warnings'])})"):
        for w in body["warnings"]:
            st.markdown(f"- {w}")
st.caption(f"Source: `{body['report_path']}/report.md` (numbers copied, nothing recomputed).")
