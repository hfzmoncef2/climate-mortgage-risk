"""Final PD evaluation on the test vintages, as pre-registered in D021 (Phase 7).

Run once. All choices were fixed on validation and committed before this module touched
the test vintages (configs/pd_model.yaml, `final`):
    primary     WoE scorecard + static coast features, usual selection, refitted on
                2004-2018, through-the-cycle anchored (D020)
    comparison  base WoE scorecard, same procedure: the climate gain on test
    benchmark   base LightGBM, refitted on 2004-2018 with the number of trees chosen by
                early stopping on validation (discrimination only)
Test vintages 2021-2024: AUC / Gini / KS with 95% bootstrap intervals; AUC differences with
paired intervals (same resampled loans for every model); calibration of the primary TTC PD
overall, by decile and by vintage.

Outputs:
    reports/pd_final.md
    reports/figures/fig12_pd_test_calibration.png

Usage:
    uv run --frozen python -m climrisk.models.final
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

from climrisk.causal import event_study as es
from climrisk.features import pd_features
from climrisk.features.target import PROCESSED_DIR
from climrisk.models.calibration import (
    apply_shift,
    calibration_table,
    fig_calibration,
    level_shift,
    recalibration,
)
from climrisk.models.gbm import GBM
from climrisk.models.metrics import classification_metrics
from climrisk.models.scorecard import Scorecard
from climrisk.models.ttc import long_run_rate
from climrisk.viz.eda import apply_style, md_table

PRIMARY, BASE, BENCHMARK = "Scorecard + coast (primary)", "Scorecard (base)", "LightGBM (base)"

log = logging.getLogger(__name__)


def bootstrap_aucs(
    y: np.ndarray, preds: dict[str, np.ndarray], n_boot: int, seed: int
) -> pd.DataFrame:
    """AUC of every model on the same resampled loans: one row per resample."""
    y = np.asarray(y)
    rng = np.random.default_rng(seed)
    rows = []
    for _ in range(n_boot):
        i = rng.integers(0, len(y), len(y))
        if y[i].min() == y[i].max():
            continue
        rows.append({name: roc_auc_score(y[i], np.asarray(p)[i]) for name, p in preds.items()})
    return pd.DataFrame(rows)


def interval(draws: pd.Series) -> tuple[float, float]:
    lo, hi = np.percentile(draws, [2.5, 97.5])
    return float(lo), float(hi)


def fit_final_models(
    cfg: dict, fit: pd.DataFrame, train: pd.DataFrame, valid: pd.DataFrame
) -> dict:
    f, fin = cfg["features"], cfg["final"]
    y_fit = fit["default_24m"].to_numpy()
    primary = Scorecard.from_config(
        cfg, f["numeric"] + list(fin["climate_features"]), f["categorical"]
    ).fit(fit, y_fit)
    base = Scorecard.from_config(cfg, f["numeric"], f["categorical"]).fit(fit, y_fit)
    tuned = GBM.from_config(cfg, f["numeric"], f["categorical"]).fit(
        train, train["default_24m"].to_numpy(), valid, valid["default_24m"].to_numpy()
    )
    gbm = GBM.from_config(cfg, f["numeric"], f["categorical"]).fit(
        fit, y_fit, n_estimators=tuned.n_trees
    )
    return {PRIMARY: primary, BASE: base, BENCHMARK: gbm}


def run(
    processed_dir: Path = PROCESSED_DIR,
    reports_dir: Path = es.REPORTS_DIR,
    cfg: dict | None = None,
) -> dict:
    cfg = cfg or pd_features.load_config()
    fin = cfg["final"]
    apply_style()
    path = str(processed_dir / "pd_features.parquet").replace("'", "''")
    with duckdb.connect() as con:
        df = con.execute(f"SELECT * FROM read_parquet('{path}')").df()
    fit = df[df["split"].isin(fin["fit_splits"])]
    train, valid = df[df["split"] == "train"], df[df["split"] == "validation"]
    test = df[df["split"] == fin["test_split"]]
    y_te = test["default_24m"].to_numpy()
    log.info("Refit on %s loans; test %s loans", f"{len(fit):,}", f"{len(test):,}")

    models = fit_final_models(cfg, fit, train, valid)
    long_run = long_run_rate(fit["vintage"], fit["default_24m"])
    shifts = {
        name: level_shift(m.predict_proba(fit), long_run)
        for name, m in models.items()
        if isinstance(m, Scorecard)
    }
    preds = {}
    for name, m in models.items():
        p = m.predict_proba(test)
        preds[name] = apply_shift(p, shifts[name]) if name in shifts else p

    draws = bootstrap_aucs(y_te, preds, int(fin["bootstrap"]), int(fin["seed"]))
    disc_rows = []
    for name, p in preds.items():
        m = classification_metrics(y_te, p)
        lo, hi = interval(draws[name])
        disc_rows.append((name, m["auc"], lo, hi, m["gini"], 2 * lo - 1, 2 * hi - 1, m["ks"]))
        log.info("%s: test AUC %.4f [%.4f, %.4f]", name, m["auc"], lo, hi)
    diff_rows = []
    for new in (PRIMARY, BENCHMARK):
        gain = roc_auc_score(y_te, preds[new]) - roc_auc_score(y_te, preds[BASE])
        diff_rows.append((f"{new} - {BASE}", gain, *interval(draws[new] - draws[BASE])))
        log.info("%s: %+.4f", diff_rows[-1][0], gain)

    p_main = preds[PRIMARY]
    table = calibration_table(y_te, p_main, int(cfg["calibration"]["bins"]))
    a, b = recalibration(y_te, p_main)
    by_vintage = (
        pd.DataFrame({"vintage": test["vintage"].to_numpy(), "y": y_te, "p": p_main})
        .groupby("vintage")
        .agg(loans=("y", "size"), defaults=("y", "sum"), rate=("y", "mean"), pd=("p", "mean"))
        .reset_index()
    )
    reports_dir.mkdir(parents=True, exist_ok=True)
    fig_calibration(
        {PRIMARY: table},
        reports_dir / "figures" / "fig12_pd_test_calibration.png",
        title="TTC PD calibration on the test vintages (2021-2024)",
    )

    card = models[PRIMARY]
    climate = [
        (feat, card.binners[feat].iv, card.dropped.get(feat, "kept"))
        for feat in fin["climate_features"]
    ]
    sections = [
        "# Final PD evaluation on the test vintages",
        "Generated by `python -m climrisk.models.final`. Models and features pre-registered in "
        "D021 before the test vintages were used. Refit on 2004-2018 "
        f"({len(fit):,} loans); test = 2021-2024 ({len(test):,} loans, {int(y_te.sum()):,} "
        f"defaults). LightGBM uses {models[BENCHMARK].n_trees} trees (early stopping on "
        f"validation). 95% bootstrap intervals over test loans ({len(draws)} resamples; ZIP3 "
        "dependence not included).",
        "## Discrimination",
        md_table(
            ["model", "auc", "auc_low", "auc_high", "gini", "gini_low", "gini_high", "ks"],
            disc_rows,
            {i: "{:.4f}" for i in range(1, 8)},
        ),
        "## AUC differences (paired)",
        md_table(
            ["comparison", "auc_diff", "ci_low", "ci_high"],
            diff_rows,
            {1: "{:+.4f}", 2: "{:+.4f}", 3: "{:+.4f}"},
        ),
        "## Climate features in the primary model",
        md_table(["feature", "iv_2004_2018", "status"], climate, {1: "{:.4f}"}),
        "## Calibration of the primary TTC PD",
        f"Long-run (TTC) rate 2004-2018: {100 * long_run:.3f}%. Mean TTC PD on test: "
        f"{100 * p_main.mean():.3f}%; observed default rate: {100 * y_te.mean():.3f}% "
        f"(observed / predicted {y_te.mean() / p_main.mean():.2f}). Level shift needed on "
        f"test a = {a:+.3f}; slope b = {b:.3f}.",
        "![Test calibration](figures/fig12_pd_test_calibration.png)",
        "### By vintage",
        md_table(
            [
                "vintage",
                "loans",
                "defaults",
                "default_rate_pct",
                "mean_ttc_pd_pct",
                "observed / predicted",
            ],
            [
                (
                    int(r.vintage),
                    int(r.loans),
                    int(r.defaults),
                    100 * r.rate,
                    100 * r.pd,
                    r.rate / r.pd,
                )
                for r in by_vintage.itertuples()
            ],
            {3: "{:.2f}", 4: "{:.2f}", 5: "{:.2f}"},
        ),
        "### By decile of TTC PD",
        md_table(
            [
                "decile",
                "loans",
                "defaults",
                "mean_pd_pct",
                "default_rate_pct",
                "observed / predicted",
            ],
            [
                (
                    int(r.bin),
                    int(r.loans),
                    int(r.defaults),
                    100 * r.mean_pd,
                    100 * r.default_rate,
                    r.default_rate / r.mean_pd,
                )
                for r in table.itertuples()
            ],
            {3: "{:.3f}", 4: "{:.3f}", 5: "{:.2f}"},
        ),
    ]
    (reports_dir / "pd_final.md").write_text("\n\n".join(sections) + "\n", encoding="utf-8")
    log.info("Wrote %s", reports_dir / "pd_final.md")
    return {
        "discrimination": disc_rows,
        "differences": diff_rows,
        "by_vintage": by_vintage,
        "models": models,
        "shifts": shifts,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()


if __name__ == "__main__":
    main()
