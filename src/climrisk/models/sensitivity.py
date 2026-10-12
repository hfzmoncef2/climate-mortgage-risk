"""PD model with the credit-default target: the sensitivity planned in D022 (Phase 7).

Same procedure as the final D004 evaluation (models/final.py, D021), with the target
replaced by `credit_default_24m` (features/credit_default.py) and the samples restricted
to the loans eligible for it:
    primary     scorecard + static coast features, refitted on 2004-2018, through-the-cycle
                anchored to the long-run credit-default rate
    comparison  base scorecard; benchmark: base LightGBM (early stopping on validation)
Evaluated on the eligible test loans (2021, 2022 and early 2023): AUC with 95% bootstrap
intervals, calibration overall and by vintage. As an addition outside D022, the D004
primary model is scored on the same test loans, so the two targets are compared on
identical loans.

Outputs:
    reports/pd_credit_sensitivity.md

Usage:
    uv run --frozen python -m climrisk.models.sensitivity
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
from climrisk.models.calibration import apply_shift, level_shift
from climrisk.models.final import (
    BASE,
    BENCHMARK,
    PRIMARY,
    bootstrap_aucs,
    fit_final_models,
    interval,
)
from climrisk.models.metrics import classification_metrics
from climrisk.models.scorecard import Scorecard
from climrisk.models.ttc import long_run_rate
from climrisk.viz.eda import md_table

D004_PRIMARY = "Scorecard + coast, D004 target (same test loans)"

log = logging.getLogger(__name__)


def load_eligible(processed_dir: Path) -> pd.DataFrame:
    """PD features of the loans eligible for the credit target, with both targets."""
    feature_path = str(processed_dir / "pd_features.parquet").replace("'", "''")
    with duckdb.connect() as con:
        features = con.execute(f"SELECT * FROM read_parquet('{feature_path}')").df()
    credit = duckdb.sql(
        f"SELECT loan_id, credit_eligible, credit_default_24m "
        f"FROM '{processed_dir / 'credit_default.parquet'}'"
    ).df()
    df = features.merge(credit, on="loan_id", how="inner", validate="one_to_one")
    return df[df["credit_eligible"]].reset_index(drop=True)


def ttc_predictions(model, fit: pd.DataFrame, test: pd.DataFrame, target: str) -> np.ndarray:
    """Test PDs shifted so that the mean fitted PD equals the long-run rate of `target`."""
    shift = level_shift(model.predict_proba(fit), long_run_rate(fit["vintage"], fit[target]))
    return apply_shift(model.predict_proba(test), shift)


def run(
    processed_dir: Path = PROCESSED_DIR,
    reports_dir: Path = es.REPORTS_DIR,
    cfg: dict | None = None,
) -> dict:
    cfg = cfg or pd_features.load_config()
    fin, f = cfg["final"], cfg["features"]
    df = load_eligible(processed_dir)
    d004 = df.copy()
    credit = df.assign(default_24m=df["credit_default_24m"])  # the models read default_24m

    def split(d: pd.DataFrame, names: list[str]) -> pd.DataFrame:
        return d[d["split"].isin(names)]

    fit, test = split(credit, fin["fit_splits"]), split(credit, [fin["test_split"]])
    train, valid = split(credit, ["train"]), split(credit, ["validation"])
    y_te = test["default_24m"].to_numpy()
    log.info(
        "Credit target: fit %s loans, test %s loans (%s credit defaults)",
        f"{len(fit):,}",
        f"{len(test):,}",
        f"{int(y_te.sum()):,}",
    )

    models = fit_final_models(cfg, fit, train, valid)
    preds = {
        name: ttc_predictions(m, fit, test, "default_24m")
        if isinstance(m, Scorecard)
        else m.predict_proba(test)
        for name, m in models.items()
    }

    # Addition outside D022: the D004 primary model on the same eligible test loans
    fit_d, test_d = split(d004, fin["fit_splits"]), split(d004, [fin["test_split"]])
    d004_model = Scorecard.from_config(
        cfg, f["numeric"] + list(fin["climate_features"]), f["categorical"]
    ).fit(fit_d, fit_d["default_24m"].to_numpy())
    p_d004 = ttc_predictions(d004_model, fit_d, test_d, "default_24m")
    y_d004 = test_d["default_24m"].to_numpy()

    draws = bootstrap_aucs(y_te, preds, int(fin["bootstrap"]), int(fin["seed"]))
    disc_rows = []
    for name, p in preds.items():
        lo, hi = interval(draws[name])
        disc_rows.append((f"{name}, credit target", roc_auc_score(y_te, p), lo, hi))
    d004_draws = bootstrap_aucs(
        y_d004, {D004_PRIMARY: p_d004}, int(fin["bootstrap"]), int(fin["seed"])
    )
    disc_rows.append(
        (D004_PRIMARY, roc_auc_score(y_d004, p_d004), *interval(d004_draws[D004_PRIMARY]))
    )
    for row in disc_rows:
        log.info("%s: AUC %.4f [%.4f, %.4f]", *row)
    diff_rows = [
        (
            f"{new} - {BASE}",
            roc_auc_score(y_te, preds[new]) - roc_auc_score(y_te, preds[BASE]),
            *interval(draws[new] - draws[BASE]),
        )
        for new in (PRIMARY, BENCHMARK)
    ]

    long_credit = long_run_rate(fit["vintage"], fit["default_24m"])
    long_d004 = long_run_rate(fit_d["vintage"], fit_d["default_24m"])
    calib = pd.DataFrame(
        {
            "vintage": test["vintage"].to_numpy(),
            "y_credit": y_te,
            "p_credit": preds[PRIMARY],
            "y_d004": y_d004,
            "p_d004": p_d004,
        }
    )
    by_vintage = (
        calib.groupby("vintage")
        .agg(
            loans=("y_credit", "size"),
            credit_rate=("y_credit", "mean"),
            credit_pd=("p_credit", "mean"),
            d004_rate=("y_d004", "mean"),
            d004_pd=("p_d004", "mean"),
        )
        .reset_index()
    )
    overall = classification_metrics(y_te, preds[PRIMARY])

    def ratio_rows(t: pd.DataFrame) -> list[tuple]:
        return [
            (
                int(r.vintage),
                int(r.loans),
                100 * r.credit_rate,
                100 * r.credit_pd,
                r.credit_rate / r.credit_pd,
                100 * r.d004_rate,
                100 * r.d004_pd,
                r.d004_rate / r.d004_pd,
            )
            for r in t.itertuples()
        ]

    total = pd.DataFrame(
        [
            {
                "vintage": 0,
                "loans": len(calib),
                "credit_rate": y_te.mean(),
                "credit_pd": preds[PRIMARY].mean(),
                "d004_rate": y_d004.mean(),
                "d004_pd": p_d004.mean(),
            }
        ]
    )
    rows = ratio_rows(by_vintage) + [("all", *ratio_rows(total)[0][1:])]

    n_fit_defaults = int(fit["default_24m"].sum())
    sections = [
        "# PD model with the credit-default target (D022 sensitivity)",
        "Generated by `python -m climrisk.models.sensitivity`. Same procedure as "
        "`reports/pd_final.md` (D021) with the credit-default target of D022, on the loans "
        f"eligible for it: refit on 2004-2018 ({len(fit):,} loans, {n_fit_defaults:,} credit "
        f"defaults), test = eligible 2021-2023 loans ({len(test):,} loans, "
        f"{int(y_te.sum()):,} credit defaults, {int(y_d004.sum()):,} D004 defaults). "
        f"LightGBM uses {models[BENCHMARK].n_trees} trees. 95% bootstrap intervals over test "
        f"loans ({len(draws)} resamples; ZIP3 dependence not included). The D004 row is an "
        "addition outside D022: the D004 primary model scored on the same test loans.",
        "## Discrimination on the eligible test loans",
        md_table(
            ["model", "auc", "auc_low", "auc_high"],
            disc_rows,
            {1: "{:.4f}", 2: "{:.4f}", 3: "{:.4f}"},
        ),
        "## AUC differences, credit target (paired)",
        md_table(
            ["comparison", "auc_diff", "ci_low", "ci_high"],
            diff_rows,
            {1: "{:+.4f}", 2: "{:+.4f}", 3: "{:+.4f}"},
        ),
        "## Calibration of the TTC PD, both targets on the same test loans",
        f"Long-run (TTC) rates 2004-2018: credit default {100 * long_credit:.3f}%, D004 "
        f"{100 * long_d004:.3f}%. Primary model, credit target: Brier "
        f"{overall['brier']:.4f}.",
        md_table(
            [
                "vintage",
                "loans",
                "credit_rate_pct",
                "credit_ttc_pd_pct",
                "credit_obs/pred",
                "d004_rate_pct",
                "d004_ttc_pd_pct",
                "d004_obs/pred",
            ],
            rows,
            {2: "{:.2f}", 3: "{:.2f}", 4: "{:.2f}", 5: "{:.2f}", 6: "{:.2f}", 7: "{:.2f}"},
        ),
    ]
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "pd_credit_sensitivity.md").write_text(
        "\n\n".join(sections) + "\n", encoding="utf-8"
    )
    log.info("Wrote %s", reports_dir / "pd_credit_sensitivity.md")
    return {"discrimination": disc_rows, "differences": diff_rows, "calibration": rows}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()


if __name__ == "__main__":
    main()
