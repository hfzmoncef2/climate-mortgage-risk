"""Climate ablation: does adding the climate features improve the PD model? (Phase 7)

Each model (WoE scorecard, LightGBM) is fitted on the training vintages without climate
features (base), then with each climate feature set of configs/pd_model.yaml
(`ablation.feature_sets`), everything else equal. In the scorecard the climate features
are forced past the IV and correlation filters (a low IV is itself a result, reported
below); a wrong-sign coefficient still removes them.
Evaluation on the validation vintages:
    AUC gain        AUC(with climate) - AUC(base), with a 95% paired bootstrap interval
                    (the same resampled loans score both models; loans are resampled
                    individually, so ZIP3 dependence and fitting noise are not included)
    coastal loans   the same comparison within ZIP3 mostly near the hurricane coast
    PSI             population stability index of each climate feature, train ->
                    validation: a feature whose distribution drifts over time can act as
                    a proxy for the vintage
The decision (which features the final model uses) is made here, on validation; the test
vintages only confirm it.

Outputs:
    reports/pd_climate_ablation.md

Usage:
    uv run --frozen python -m climrisk.models.ablation
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

from climrisk.causal import event_study as es
from climrisk.features import pd_features
from climrisk.features.target import PROCESSED_DIR
from climrisk.models.gbm import GBM
from climrisk.models.metrics import classification_metrics, psi
from climrisk.models.scorecard import Scorecard, load_features
from climrisk.viz.eda import md_table

log = logging.getLogger(__name__)


def paired_auc_gain(
    y: np.ndarray, p_base: np.ndarray, p_new: np.ndarray, n_boot: int, seed: int
) -> tuple[float, float, float]:
    """AUC(p_new) - AUC(p_base) and its 95% percentile interval (paired bootstrap)."""
    y, p_base, p_new = (np.asarray(a) for a in (y, p_base, p_new))
    gain = roc_auc_score(y, p_new) - roc_auc_score(y, p_base)
    rng = np.random.default_rng(seed)
    draws = []
    for _ in range(n_boot):
        i = rng.integers(0, len(y), len(y))
        if y[i].min() == y[i].max():  # a resample without both classes has no AUC
            continue
        draws.append(roc_auc_score(y[i], p_new[i]) - roc_auc_score(y[i], p_base[i]))
    lo, hi = np.percentile(draws, [2.5, 97.5])
    return float(gain), float(lo), float(hi)


def fit_variants(
    kind: str, cfg: dict, train: pd.DataFrame, valid: pd.DataFrame
) -> dict[str, Scorecard | GBM]:
    """{'base': model, <feature set name>: model with that climate set, ...}."""
    f = cfg["features"]
    variants = {"base": []} | dict(cfg["ablation"]["feature_sets"])
    y_tr, y_va = train["default_24m"].to_numpy(), valid["default_24m"].to_numpy()
    out = {}
    for name, extra in variants.items():
        numeric = f["numeric"] + list(extra)
        if kind == "scorecard":
            card = Scorecard.from_config(cfg, numeric, f["categorical"])
            card.force = list(extra)  # tested even if their IV is below min_iv
            out[name] = card.fit(train, y_tr)
        else:
            out[name] = GBM.from_config(cfg, numeric, f["categorical"]).fit(
                train, y_tr, valid, y_va
            )
    return out


def run(
    processed_dir: Path = PROCESSED_DIR,
    reports_dir: Path = es.REPORTS_DIR,
    cfg: dict | None = None,
) -> dict:
    cfg = cfg or pd_features.load_config()
    a = cfg["ablation"]
    n_boot, seed = int(a["bootstrap"]), int(a["seed"])
    df = load_features(processed_dir)
    train, valid = df[df["split"] == "train"], df[df["split"] == "validation"]
    y_va = valid["default_24m"].to_numpy()
    coastal = (valid["near_coast_share"] >= float(a["coastal_share"])).to_numpy()
    groups = (("all validation loans", np.ones_like(coastal)), ("coastal ZIP3", coastal))

    perf_rows, gain_rows, results = [], [], {}
    for kind, label in (("scorecard", "WoE scorecard"), ("gbm", "LightGBM")):
        models = fit_variants(kind, cfg, train, valid)
        p = {name: m.predict_proba(valid) for name, m in models.items()}
        for name in models:
            m = classification_metrics(y_va, p[name])
            perf_rows.append((label, name, m["auc"], m["gini"], m["ks"], m["brier"]))
        for name in (n for n in models if n != "base"):
            for group, mask in groups:
                gain = paired_auc_gain(y_va[mask], p["base"][mask], p[name][mask], n_boot, seed)
                gain_rows.append(
                    (label, name, group, int(mask.sum()), int(y_va[mask].sum()), *gain)
                )
                log.info("%s + %s, %s: AUC gain %+.4f [%+.4f, %+.4f]", label, name, group, *gain)
        results[kind] = models

    widest = max(a["feature_sets"], key=lambda k: len(a["feature_sets"][k]))
    card = results["scorecard"][widest]
    status = card.summary().set_index("feature")
    gbm = results["gbm"][widest].importance().set_index("feature")["gain_share"]
    bins = int(a["psi_bins"])
    climate_rows = [
        (
            feat,
            float(status.loc[feat, "iv"]),
            status.loc[feat, "status"],
            float(gbm.get(feat, 0.0)),
            float(train[feat].mean()),
            float(valid[feat].mean()),
            psi(train[feat].to_numpy(), valid[feat].to_numpy(), bins),
        )
        for feat in a["feature_sets"][widest]
    ]

    sections = [
        "# Climate ablation of the PD model",
        "Generated by `python -m climrisk.models.ablation`. Same models, fitted on the training "
        "vintages without climate features (base) and with each climate feature set; evaluated "
        f"on the validation vintages. Gains with a 95% paired bootstrap interval ({n_boot} "
        "resamples of individual loans: ZIP3 dependence, model fitting and validation-based "
        "early stopping are not included). Test vintages not used.",
        "Feature sets: "
        + "; ".join(f"`{k}` = {', '.join(v)}" for k, v in a["feature_sets"].items())
        + ".",
        "## Performance on validation",
        md_table(
            ["model", "features", "auc", "gini", "ks", "brier"],
            perf_rows,
            {i: "{:.4f}" for i in range(2, 6)},
        ),
        "## AUC gain over the base model",
        f"Coastal ZIP3: at least {float(a['coastal_share']):.0%} of homes within 50 km of the "
        "hurricane coast. A subgroup AUC only measures ranking within the subgroup.",
        md_table(
            [
                "model",
                "features",
                "subsample",
                "loans",
                "defaults",
                "auc_gain",
                "ci_low",
                "ci_high",
            ],
            gain_rows,
            {5: "{:+.4f}", 6: "{:+.4f}", 7: "{:+.4f}"},
        ),
        f"## The climate features (`{widest}` models)",
        f"Scorecard: information value on train (usual threshold {card.min_iv:g}) and status "
        "after the sign check. LightGBM: share of total gain. PSI train -> validation: "
        "< 0.1 stable, 0.1-0.25 moderate shift, > 0.25 large shift.",
        md_table(
            [
                "feature",
                "scorecard_iv",
                "scorecard_status",
                "lightgbm_gain_share",
                "mean_train",
                "mean_validation",
                "psi",
            ],
            climate_rows,
            {1: "{:.4f}", 3: "{:.4f}", 4: "{:.3f}", 5: "{:.3f}", 6: "{:.3f}"},
        ),
    ]
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "pd_climate_ablation.md").write_text(
        "\n\n".join(sections) + "\n", encoding="utf-8"
    )
    log.info("Wrote %s", reports_dir / "pd_climate_ablation.md")
    return {"performance": perf_rows, "gains": gain_rows, "climate": climate_rows}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()


if __name__ == "__main__":
    main()
