"""Two model versions side by side: by default the Champion model (A) and the Challenger (B)."""

import data
import shaping
import streamlit as st

st.set_page_config(page_title="Compare versions", layout="wide")
st.title("Compare versions")

listed = data.listed_versions()
entries = listed["models"]
by_label = {data.describe(entry): entry["version"] for entry in entries}
labels = list(by_label)
versions = [entry["version"] for entry in entries]


def _default(version: str | None, fallback: int) -> int:
    return versions.index(version) if version in versions else min(fallback, len(labels) - 1)


pick_a, pick_b = st.columns(2)
a = by_label[pick_a.selectbox("A", labels, index=_default(listed["champion"], 0))]
b = by_label[pick_b.selectbox("B", labels, index=_default(listed["challenger"], 1))]

if a == b:
    st.info("Pick two different versions.")
    st.stop()

reports = {version: data.evaluation(version) for version in (a, b)}
missing = [version for version, report in reports.items() if report is None]
if missing:
    st.warning(
        f"No evaluation report is saved with {', '.join(missing)}. Make one with "
        "`txrisk evaluate --scorer bundle --bundle models/<version>`."
    )
    st.stop()
both = {version: report for version, report in reports.items() if report is not None}

st.subheader("Metrics")
table = shaping.compare_headlines(both[a], both[b]).rename(columns={"A": a, "B": b})
st.dataframe(table, use_container_width=True)

st.subheader("Curves")
pr, roc = st.columns(2)
for column, kind, title in ((pr, "pr", "Precision-recall"), (roc, "roc", "ROC")):
    column.markdown(f"**{title}**")
    curve = shaping.curve_frame(both, kind)
    if curve.empty:
        column.caption("Not recorded with these reports.")
    else:
        column.line_chart(curve)

st.subheader("Recall at High by Fraud scenario")
scenarios = shaping.scenario_recall(both)
st.bar_chart(scenarios[[a, b]], stack=False)
st.dataframe(scenarios, use_container_width=True)

st.subheader("By segment")
metric = st.selectbox(
    "Metric",
    ["false_positive_rate", "recall", "precision"],
    format_func=lambda m: m.replace("_", " ").capitalize() + " at High",
)
names = list(both[a]["segments"])
tabs = st.tabs(names)
for tab, name in zip(tabs, names, strict=True):
    tab.dataframe(shaping.segment_comparison(both, name, metric), use_container_width=True)
