"""One model version: its metrics, PR and ROC curves, recall by Fraud scenario and by segment,
and recall against Ground truth and against discovered Fraud labels."""

import data
import shaping
import streamlit as st

st.set_page_config(page_title="Model version", layout="wide")
st.title("Model version")

listed = data.listed_versions()
by_label = {data.describe(entry): entry["version"] for entry in listed["models"]}
version = by_label[st.selectbox("Version", list(by_label))]

report = data.evaluation(version)
card = data.card(version)

if report is None:
    st.warning(
        f"No evaluation report is saved with {version}. Make one with "
        f"`txrisk evaluate --scorer bundle --bundle models/{version}`. "
        "The model card's metrics are below."
    )
else:
    st.caption(
        f"{report['transactions']:,} Transactions on days {report['days'][0]}-{report['days'][1]}, "
        f"{report['fraud']:,} Fraud ({report['fraud_discovered']:,} discovered by the end)."
    )
    st.subheader("Metrics")
    st.dataframe(shaping.headline(report).rename("Value").to_frame(), use_container_width=True)

    st.subheader("Recall: Ground truth against discovered labels")
    recall = report["recall_against"]
    gap = st.columns(3)
    gap[0].metric("Ground truth", f"{recall['ground_truth']:.3f}")
    gap[1].metric("Discovered labels", f"{recall['discovered_labels']:.3f}")
    gap[2].metric(
        "Fraud never discovered",
        f"{1 - report['fraud_discovered'] / report['fraud']:.1%}" if report["fraud"] else "n/a",
    )
    st.caption(
        "Ground truth is everything that happened; discovered labels are only what the "
        "operation would know by the end of the evaluation period."
    )

    st.subheader("Curves")
    pr, roc = st.columns(2)
    for column, kind, title in ((pr, "pr", "Precision-recall"), (roc, "roc", "ROC")):
        curve = shaping.curve_frame({version: report}, kind)
        column.markdown(f"**{title}**")
        if curve.empty:
            column.caption("Not recorded with this report.")
        else:
            column.line_chart(
                curve,
                x_label=report["curves"][kind]["x_label"],
                y_label=report["curves"][kind]["y_label"],
            )

    st.subheader("Recall at High by Fraud scenario")
    scenarios = shaping.scenario_recall({version: report})
    st.bar_chart(scenarios[[version]])
    st.dataframe(scenarios, use_container_width=True)

    st.subheader("By segment")
    names = list(report["segments"])
    tabs = st.tabs(names)
    for tab, name in zip(tabs, names, strict=True):
        tab.dataframe(shaping.segment_table(report, name), use_container_width=True)

st.subheader("Model card metrics")
overall = shaping.card_overall(card)
if overall is None:
    st.caption("Not recorded with this version.")
else:
    st.caption(card["metrics"]["evaluated_on"])
    st.dataframe(overall.rename("Value").to_frame(), use_container_width=True)
    for name in card["metrics"]["by_segment"]:
        with st.expander(f"Card: by {name}"):
            st.dataframe(shaping.card_segments(card, name), use_container_width=True)
