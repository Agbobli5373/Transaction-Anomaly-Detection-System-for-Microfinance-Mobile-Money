"""The model card: what one model version is for, what it learned from, how it does on each
segment and where it should not be trusted, written when the version is trained and saved with it.

A model risk reviewer reads this instead of the code. It holds only what the system itself knew
when it trained the version (ADR 0007): the segment metrics are against the Fraud labels known on
the day the version went live, on the calibration days, which the supervised and Anomaly models
never trained on (the stacker did). The out-of-sample comparison with the Champion model is the
backtest gate, run when Promotion is asked for.
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from txrisk.models.bundle import ModelBundle

CARD_FILE = "model_card.json"
MARKDOWN_FILE = "model_card.md"

INTENDED_USE = [
    "Rank Transactions by how likely they are to be fraud that the operation would discover, so "
    "that Analysts look at the few worth looking at first (the Alert budget).",
    "Advisory only (ADR 0001): a Risk score raises Alerts and supports Cases. It never blocks, "
    "holds or reverses a Transaction and never declares fraud.",
    "Used as the Champion model only after a person has promoted it, having seen it pass the "
    "backtest and shadow gates.",
    "Not for credit decisions, for judging a person, or for any market other than the one it "
    "was trained on.",
]

LIMITATIONS = [
    "Trained and tested on a synthetic Ghana-style mobile money dataset (ADR 0002, ADR 0003). "
    "Nothing here shows how it would perform on a real operator's data.",
    "The Risk score is the chance of discovered fraud x 100, not of fraud. Fraud labels arrive "
    "1-30 days late and only some fraud is ever discovered, so what the operation never finds is "
    "learned as legitimate.",
    "Fraud that is discovered only through Cases is biased towards what the system Alerted "
    "already, so a pattern it misses is under-represented in its own labels.",
    "A new Fraud scenario or a shift in an existing one is not known to the supervised model "
    "until it has been discovered and the model retrained; drift monitoring is the early "
    "warning.",
    "Wallets with fewer than the minimum history are scored with history features empty; being "
    "new is itself a signal, so a legitimate new customer is more likely to be flagged.",
    "Segment metrics rest on few Fraud labels in small segments, and are noisy there.",
    "No institutional model risk sign-off, external audit or regulatory approval stands behind "
    "it (ADR 0003).",
]

PRIVACY = [
    "Wallets are pseudonymised identifiers (a keyed hash of the phone number); phone numbers and "
    "customer names are never stored or used.",
    "The inputs each Transaction was scored with are kept 60 days, Transactions, assessments, "
    "Alerts and Fraud labels 365, and the audit log at least a year (config/retention.toml).",
    "Ground truth never reaches training or serving (ADR 0007). This is a simulation on "
    "synthetic data, not a claim of compliance with any data-protection law.",
]


def build_card(
    bundle: "ModelBundle",
    *,
    data: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The card of `bundle`: what is known of it from the bundle itself, plus the `data` it was
    trained on and the `metrics` it was judged by when training made them (older bundles have
    neither, and say so)."""
    return {
        "version": bundle.version,
        "intended_use": INTENDED_USE,
        "model": {
            "components": [
                "Rules score (declarative Rule hits, by severity)",
                "Anomaly score (Isolation Forest, unsupervised, as a percentile of training)",
                "Supervised fraud probability (LightGBM, class-weighted, isotonic calibration)",
            ],
            "stacker": {
                "weights": bundle.stacker.weights,
                "intercept": bundle.stacker.intercept,
            },
            "thresholds": {"medium": bundle.thresholds.medium, "high": bundle.thresholds.high},
            "inputs": list(bundle.input_columns),
        },
        "data": {
            "fitted_on_days": bundle.fitted_on,
            **(data or {"recorded": False}),
            "privacy": PRIVACY,
        },
        "metrics": metrics or {"recorded": False},
        "limitations": LIMITATIONS,
    }


def render_markdown(card: dict[str, Any]) -> str:
    """The card as a Markdown page."""
    lines = [f"# Model card: {card['version']}", "", "## Intended use", ""]
    lines += [f"- {item}" for item in card["intended_use"]]

    model = card["model"]
    lines += ["", "## Model", ""]
    lines += [f"- {item}" for item in model["components"]]
    weights = ", ".join(
        f"{name} {value:.2f}" for name, value in model["stacker"]["weights"].items()
    )
    lines += [
        f"- Stacker weights: {weights}; intercept {model['stacker']['intercept']:.2f}",
        f"- Risk band thresholds (Risk score): Medium from {model['thresholds']['medium']:.3g}, "
        f"High from {model['thresholds']['high']:.3g}, set from the Alert budget",
        f"- {len(model['inputs'])} model inputs",
    ]

    data = card["data"]
    lines += ["", "## Data", ""]
    fitted = data["fitted_on_days"]
    lines.append(
        "- Days (from the period start): "
        f"models {_days(fitted.get('models'))}, stacker {_days(fitted.get('stacker'))}, "
        f"thresholds {_days(fitted.get('thresholds'))}; labels known on day "
        f"{fitted.get('labels_known_on_day')}"
    )
    for key, label in (
        ("transactions_by_split", "Transactions by split"),
        ("fraud_labels_known", "Fraud labels known at the cutoff"),
        ("label_maturity_days", "Label maturity window (days)"),
        ("alert_budget_per_day", "Alert budget (Alerts a day)"),
    ):
        if key in data:
            lines.append(f"- {label}: {data[key]}")
    if data.get("recorded") is False:
        lines.append("- What this version was trained on was not recorded with it.")
    lines += [f"- {item}" for item in data["privacy"]]

    metrics = card["metrics"]
    lines += ["", "## Metrics by segment", ""]
    if metrics.get("recorded") is False:
        lines.append("Not recorded with this version.")
    else:
        lines += [metrics["evaluated_on"], ""]
        lines += _table("All", {"all": metrics["overall"]}, with_ranking=True)
        for segment, groups in metrics["by_segment"].items():
            lines += _table(segment, groups)

    lines += ["", "## Limitations", ""]
    lines += [f"- {item}" for item in card["limitations"]]
    return "\n".join(lines) + "\n"


def _days(window: list[int] | None) -> str:
    return "n/a" if window is None else f"{window[0]}-{window[1]}"


def _number(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _table(title: str, groups: dict[str, Any], *, with_ranking: bool = False) -> list[str]:
    extra = " PR-AUC | ROC-AUC |" if with_ranking else ""
    rule = " --- | --- |" if with_ranking else ""
    lines = [
        f"### {title}",
        "",
        "| Segment | Transactions | Fraud | Precision at High | Recall at High | "
        f"False positive rate at High |{extra}",
        f"| --- | --- | --- | --- | --- | --- |{rule}",
    ]
    for name, row in groups.items():
        cells = [
            name,
            str(row["transactions"]),
            str(row["fraud"]),
            _number(row["precision"]),
            _number(row["recall"]),
            _number(row["false_positive_rate"]),
        ]
        if with_ranking:
            cells += [_number(row.get("pr_auc")), _number(row.get("roc_auc"))]
        lines.append("| " + " | ".join(cells) + " |")
    return [*lines, ""]


def write_card(card: dict[str, Any], folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / CARD_FILE).write_text(json.dumps(card, indent=2) + "\n", encoding="utf-8")
    (folder / MARKDOWN_FILE).write_text(render_markdown(card), encoding="utf-8")


def load_card(folder: Path) -> dict[str, Any]:
    """The card saved in a bundle's `folder`. A bundle saved before cards existed has none, so
    one is made from the bundle itself, saying what was not recorded. Raises `FileNotFoundError`
    if the folder holds no bundle."""
    saved = folder / CARD_FILE
    if saved.exists():
        card: dict[str, Any] = json.loads(saved.read_text(encoding="utf-8"))
        return card
    from txrisk.models.bundle import ModelBundle

    return build_card(ModelBundle.load(folder))
