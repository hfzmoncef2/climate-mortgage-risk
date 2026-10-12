"""Is climate risk priced? Spread and default regressions with the same controls (Phase 6).

For each loan (30-year, non-HARP):
    spread = original rate - Freddie Mac PMMS 30-year average of the origination month
Regressions, with fixed effects state x origination month and SE clustered by ZIP3:
    spread_bp   ~ climate + controls | state^month    (all vintages; also by period)
    default_pp  ~ climate + controls | state^month    (PD sample, 24-month default x 100)
Climate variables:
    exposure_main     expected disasters per home in the previous 10 years (D009, D011)
    near_coast_share  share of the ZIP3's homes within 50 km of the hurricane coast
Controls: FICO, LTV and DTI bins, log loan amount, purpose, occupancy, property type,
units, channel, borrowers, first-time buyer, mortgage insurance, super-conforming.

Conditional associations between climate, spreads and defaults do not establish
causal pricing effects or identify which party ultimately bears the risk.

Outputs:
    reports/pricing.md
    reports/figures/fig9_pricing_vs_default.png

Usage:
    uv run --frozen python -m climrisk.causal.pricing
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pyfixest as pf
import yaml
from scipy import stats

from climrisk.causal import event_study as es
from climrisk.features.target import PROCESSED_DIR
from climrisk.ingest.freddie import ROOT, _sql_str
from climrisk.viz.eda import COLORS, apply_style, md_table

CONFIG_PATH = ROOT / "configs" / "pricing.yaml"
PMMS_PATH = ROOT / "data" / "interim" / "fred" / "pmms_monthly.parquet"
CLIMATE_VARS = ["exposure_main", "near_coast_share"]
LABELS = {
    "exposure_main": "+1 past disaster per home\n(previous 10 years)",
    "near_coast_share": "ZIP3 fully within 50 km\nof the hurricane coast (vs none)",
}
CONTROLS = (
    "C(fico_bin) + C(ltv_bin) + C(dti_bin) + log_upb + C(loan_purpose) + C(occupancy)"
    " + C(property_type) + C(num_units) + C(channel) + C(num_borrowers)"
    " + C(first_time_buyer) + has_mi + C(super_conforming)"
)

log = logging.getLogger(__name__)


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def loans_query(cfg: dict, processed_dir: Path, pmms_path: Path) -> str:
    p = cfg["pricing"]
    b = p["bins"]
    lag = int(p["origination_lag_months"])

    def binned(col: str, width: int) -> str:
        return f"coalesce(CAST(floor({col} / {width}) * {width} AS VARCHAR), 'missing')"

    def cat(col: str) -> str:
        return f"coalesce(CAST({col} AS VARCHAR), 'missing')"

    return f"""
WITH base AS (
    SELECT *,
           CAST(first_payment_date - INTERVAL {lag} MONTH AS DATE) AS orig_month
    FROM read_parquet({_sql_str(processed_dir / "loans.parquet")})
    WHERE orig_term = {int(p["orig_term"])}
      AND NOT is_harp
      AND zip3 IS NOT NULL
      AND orig_rate IS NOT NULL
)
SELECT
    b.loan_id, b.vintage, b.zip3, b.state,
    strftime(b.orig_month, '%Y-%m') AS orig_month,
    100.0 * (b.orig_rate - m.pmms_rate) AS spread_bp,
    100.0 * b.default_24m AS default_pp,
    b.in_pd_sample,
    c.exposure_main,
    k.near_coast_share,
    {binned("b.fico", b["fico"])} AS fico_bin,
    {binned("b.ltv", b["ltv"])} AS ltv_bin,
    {binned("b.dti", b["dti"])} AS dti_bin,
    ln(b.orig_upb) AS log_upb,
    {cat("b.loan_purpose")} AS loan_purpose,
    {cat("b.occupancy")} AS occupancy,
    {cat("b.property_type")} AS property_type,
    {cat("b.num_units")} AS num_units,
    {cat("b.channel")} AS channel,
    {cat("b.num_borrowers")} AS num_borrowers,
    {cat("b.first_time_buyer")} AS first_time_buyer,
    CAST(coalesce(b.mi_pct, 0) > 0 AS INTEGER) AS has_mi,
    {cat("b.super_conforming")} AS super_conforming
FROM base b
JOIN read_parquet({_sql_str(pmms_path)}) m ON m.month = b.orig_month
JOIN read_parquet({_sql_str(processed_dir / "zip3_month_climate.parquet")}) c
  ON c.zip3 = b.zip3 AND c.month = b.orig_month
JOIN read_parquet({_sql_str(processed_dir / "zip3_coast.parquet")}) k ON k.zip3 = b.zip3
"""


def fit(df: pd.DataFrame, outcome: str, name: str) -> dict:
    """One regression; returns the climate coefficients with 95% CI (t with G - 1 df)."""
    formula = f"{outcome} ~ {' + '.join(CLIMATE_VARS)} + {CONTROLS} | state^orig_month"
    model = pf.feols(formula, data=df, vcov={"CRV1": "zip3"})
    tidy = model.tidy()
    clusters = df["zip3"].nunique()
    t_crit = stats.t.ppf(0.975, max(clusters - 1, 1))
    out = {
        "name": name,
        "outcome": outcome,
        "n": int(model._N),
        "mean": float(df[outcome].mean()),
        "clusters": clusters,
        "coef": {},
    }
    for var in CLIMATE_VARS:
        est, se = float(tidy.loc[var, "Estimate"]), float(tidy.loc[var, "Std. Error"])
        p = float(tidy.loc[var, "Pr(>|t|)"])
        out["coef"][var] = (est, se, est - t_crit * se, est + t_crit * se, p)
    return out


def fig_pricing_vs_default(spread: dict, default: dict, path: Path) -> None:
    fig, axes = es.plt_subplots(1, 2, figsize=(9, 3.0), sharey=True)
    y = np.arange(len(CLIMATE_VARS))[::-1]
    for ax, res, unit in ((axes[0], spread, "bp"), (axes[1], default, "pp")):
        for yi, var in zip(y, CLIMATE_VARS, strict=True):
            est, _, lo, hi, _ = res["coef"][var]
            ax.plot([lo, hi], [yi, yi], color=COLORS["focus"], linewidth=2)
            ax.plot(est, yi, "o", color=COLORS["focus"], markersize=6)
            ax.annotate(
                f"{est:+.2f} {unit}",
                (est, yi),
                xytext=(0, 8),
                textcoords="offset points",
                ha="center",
                fontsize=8.5,
                color=COLORS["ink"],
            )
        ax.axvline(0, color=COLORS["axis"], linewidth=0.8)
        ax.set_ylim(-0.6, len(CLIMATE_VARS) - 0.4)
        ax.grid(axis="y", visible=False)
        ax.grid(axis="x", visible=True)
    axes[0].set_yticks(y, [LABELS[v] for v in CLIMATE_VARS], fontsize=8.5)
    axes[0].set_title(f"Spread over PMMS (bp)\nmean {spread['mean']:.0f} bp", fontsize=10)
    axes[1].set_title(f"24-month default (pp)\nmean {default['mean']:.2f}%", fontsize=10)
    fig.suptitle(
        "Climate exposure: effect on the rate charged vs. effect on default risk",
        x=0.02,
        ha="left",
        fontweight="bold",
        fontsize=11.5,
    )
    fig.text(
        0.02,
        -0.04,
        "Same controls (FICO, LTV, DTI, amount, purpose, occupancy, property, "
        "channel, borrowers, MI) and state x origination-month fixed effects; "
        "95% CI clustered by ZIP3.",
        fontsize=8,
        color=COLORS["muted"],
    )
    fig.tight_layout()
    es.save(fig, path)


def run(
    processed_dir: Path = PROCESSED_DIR,
    pmms_path: Path = PMMS_PATH,
    reports_dir: Path = es.REPORTS_DIR,
    cfg: dict | None = None,
) -> dict:
    cfg = cfg or load_config()
    apply_style()
    reports_dir.mkdir(parents=True, exist_ok=True)
    with duckdb.connect() as con:
        df = con.execute(loans_query(cfg, processed_dir, pmms_path)).df()
    log.info("Pricing sample: %s loans", f"{len(df):,}")

    split = int(cfg["pricing"]["period_split"])
    pd_df = df[df["in_pd_sample"] & df["default_pp"].notna()]
    results = {
        "spread_all": fit(df, "spread_bp", "Spread, all originations"),
        "spread_early": fit(df[df["vintage"] < split], "spread_bp", f"Spread, before {split}"),
        "spread_late": fit(df[df["vintage"] >= split], "spread_bp", f"Spread, from {split}"),
        "spread_pd": fit(pd_df, "spread_bp", "Spread, PD sample"),
        "default_pd": fit(pd_df, "default_pp", "24-month default, PD sample"),
    }
    fig_pricing_vs_default(
        results["spread_pd"],
        results["default_pd"],
        reports_dir / "figures" / "fig9_pricing_vs_default.png",
    )

    sd = df[CLIMATE_VARS].std()
    rows = []
    for res in results.values():
        unit = "bp" if res["outcome"] == "spread_bp" else "pp"
        for var in CLIMATE_VARS:
            est, se, lo, hi, p = res["coef"][var]
            row = (res["name"], var, unit, est, se, lo, hi, p, est * sd[var], res["mean"], res["n"])
            rows.append(row)
    sections = [
        "# Is climate risk priced?",
        "Generated by `python -m climrisk.causal.pricing`. Spread = original rate minus the "
        "Freddie Mac PMMS 30-year average of the origination month (first payment - "
        f"{cfg['pricing']['origination_lag_months']} months). 30-year non-HARP loans. Same "
        "controls in every regression; fixed effects state x origination month; 95% CI "
        "clustered by ZIP3. Associations conditional on observables, not causal effects.",
        "![Pricing vs default](figures/fig9_pricing_vs_default.png)",
        "## Coefficients",
        md_table(
            [
                "regression",
                "variable",
                "unit",
                "estimate",
                "se",
                "ci_low",
                "ci_high",
                "p_value",
                "per_1_sd",
                "outcome_mean",
                "n_loans",
            ],
            rows,
            {
                3: "{:.3f}",
                4: "{:.3f}",
                5: "{:.3f}",
                6: "{:.3f}",
                7: "{:.3f}",
                8: "{:.3f}",
                9: "{:.3f}",
            },
        ),
        "`estimate` is per unit of the variable (+1 expected past disaster per home; ZIP3 "
        "share near the coast from 0 to 1); `per_1_sd` rescales it to one standard deviation "
        "of the variable in the pricing sample.",
        "## Scale of the climate variables (pricing sample)",
        md_table(
            ["variable", "mean", "std"],
            [(v, float(df[v].mean()), float(sd[v])) for v in CLIMATE_VARS],
            {1: "{:.3f}", 2: "{:.3f}"},
        ),
    ]
    (reports_dir / "pricing.md").write_text("\n\n".join(sections) + "\n", encoding="utf-8")
    log.info("Wrote %s", reports_dir / "pricing.md")
    return results


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()


if __name__ == "__main__":
    main()
