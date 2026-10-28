"""Model versions: the Champion and Challenger models, every version retraining has made, and
the Promotions and rollbacks. The other pages show one version's evaluation and compare two."""

import data
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Model versions", layout="wide")
st.title("Model versions")
st.caption(
    "Read through the txrisk API only. Evaluation reports are offline: they judge a version on "
    "the evaluation days against Ground truth and against discovered Fraud labels."
)

listed = data.listed_versions()
left, right = st.columns(2)
left.metric("Champion model", listed["champion"] or "none")
right.metric("Challenger model", listed["challenger"] or "none")

versions = pd.DataFrame(
    [
        {
            "Version": entry["version"],
            "Roles": ", ".join(entry["roles"]),
            "Trained cutoff": entry["trained_cutoff"],
            "Trigger": entry["trigger"],
            "Evaluation report": "yes" if data.evaluation(entry["version"]) else "no",
        }
        for entry in listed["models"]
    ]
)
st.subheader("Versions")
st.dataframe(versions, hide_index=True, use_container_width=True)

st.subheader("Promotions and rollbacks")
if listed["history"]:
    history = pd.DataFrame(listed["history"]).drop(columns=["promotion_id", "gates"])
    st.dataframe(history, hide_index=True, use_container_width=True)
else:
    st.caption("None yet.")
