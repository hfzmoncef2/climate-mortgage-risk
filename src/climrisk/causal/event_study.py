"""Stacked event study: effect of climate disasters on mortgage delinquency (Phase 5).

Design (DECISIONS.md):
- An event is a ZIP3-month where at least `hit_share_threshold` of the ZIP3's homes are in
  counties with a qualifying disaster (configs/climate.yaml, main definition).
- Clean events: no other hit in the ZIP3 during the window [-before, +after], except the
  event itself and the following `grace_months` (same storm). Windows must lie inside the
  data and must not overlap the COVID forbearance period.
- One stack per event month. Controls of a stack: ZIP3 of the same Census division with no
  hit at all during the window.
- Regression, weighted by the number of loans:
      rate_{z,t,s} = a_{z,s} + g_{division,t,s} + sum_{k != -1} b_k 1[treated_{z,s}] 1[t - e_s = k]
  with standard errors clustered by ZIP3. Clean controls avoid already-treated
  comparison units. Causal interpretation
  still requires parallel trends, no spillovers and appropriate aggregation weights.

Outputs:
    reports/event_study.md
    reports/event_study_coefficients.csv
    reports/figures/fig6_event_study_dlq60.png
    reports/figures/fig7_event_study_outcomes.png

Usage:
    uv run --frozen python -m climrisk.causal.event_study
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pyfixest as pf
import yaml
from scipy import stats

from climrisk.features.target import PROCESSED_DIR
from climrisk.ingest.freddie import ROOT
from climrisk.viz.eda import COLORS, apply_style, md_table

CONFIG_PATH = ROOT / "configs" / "event_study.yaml"
REPORTS_DIR = ROOT / "reports"

# Census divisions by state FIPS code (50 states + DC). Territories have no division and
# are dropped from the event study (no comparable controls).
CENSUS_DIVISIONS = {
    "New England": ["09", "23", "25", "33", "44", "50"],
    "Middle Atlantic": ["34", "36", "42"],
    "East North Central": ["17", "18", "26", "39", "55"],
    "West North Central": ["19", "20", "27", "29", "31", "38", "46"],
    "South Atlantic": ["10", "11", "12", "13", "24", "37", "45", "51", "54"],
    "East South Central": ["01", "21", "28", "47"],
    "West South Central": ["05", "22", "40", "48"],
    "Mountain": ["04", "08", "16", "30", "32", "35", "49", "56"],
    "Pacific": ["02", "06", "15", "41", "53"],
}

log = logging.getLogger(__name__)


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def register_views(con: duckdb.DuckDBPyConnection, processed_dir: Path) -> None:
    for view, name in (
        ("climate", "zip3_month_climate"),
        ("panel", "zip3_month"),
        ("weights", "zip3_county_weights"),
    ):
        con.execute(
            f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM "
            f"read_parquet('{processed_dir / (name + '.parquet')}')"
        )
    rows = [(state, div) for div, states in CENSUS_DIVISIONS.items() for state in states]
    con.execute("CREATE OR REPLACE TABLE divisions (state_fips VARCHAR, division VARCHAR)")
    con.executemany("INSERT INTO divisions VALUES (?, ?)", rows)
    # Each ZIP3 is assigned to the state of the county holding most of its homes.
    con.execute("""
        CREATE OR REPLACE TABLE zip3_division AS
        SELECT w.zip3, d.division
        FROM (SELECT zip3, arg_max(substr(county_fips, 1, 2), weight) AS state_fips
              FROM weights GROUP BY zip3) w
        JOIN divisions d USING (state_fips)
    """)


def stacked_query(cfg: dict, threshold: float) -> str:
    """SQL for the stacked panel: one row per (stack, ZIP3, month) with outcome counts."""
    before, after = int(cfg["window"]["before"]), int(cfg["window"]["after"])
    grace = int(cfg["treatment"]["grace_months"])
    hit = f"hit_share_{cfg['treatment']['definition']}"
    clean = f"hit_share_{cfg['clean_definition']}"
    start, end = date.fromisoformat(cfg["data_start"]), date.fromisoformat(cfg["data_end"])
    covid_start = date.fromisoformat(cfg["exclude_window"]["start"])
    covid_end = date.fromisoformat(cfg["exclude_window"]["end"])
    counts = ", ".join(f"p.{col}" for col in sorted(set(cfg["outcomes"].values())))
    return f"""
WITH candidate AS (
    SELECT c.zip3, c.month AS event_month
    FROM climate c JOIN zip3_division USING (zip3)
    WHERE c.{hit} >= {threshold}
      AND c.month - INTERVAL {before} MONTH >= DATE '{start}'
      AND c.month + INTERVAL {after} MONTH <= DATE '{end}'
      AND NOT (c.month - INTERVAL {before} MONTH <= DATE '{covid_end}'
               AND c.month + INTERVAL {after} MONTH >= DATE '{covid_start}')
),
events AS (
    -- clean: no other hit in the window, except the event month and its grace months
    SELECT e.*
    FROM candidate e
    WHERE NOT EXISTS (
        SELECT 1 FROM climate h
        WHERE h.zip3 = e.zip3 AND h.{clean} > 0
          AND h.month BETWEEN e.event_month - INTERVAL {before} MONTH
                          AND e.event_month + INTERVAL {after} MONTH
          AND NOT (h.month BETWEEN e.event_month
                               AND e.event_month + INTERVAL {grace} MONTH)
    )
),
stacks AS (
    SELECT DISTINCT e.event_month, d.division
    FROM events e JOIN zip3_division d USING (zip3)
),
units AS (
    SELECT event_month, zip3, 1 AS treated FROM events
    UNION ALL
    SELECT s.event_month, z.zip3, 0 AS treated
    FROM stacks s
    JOIN zip3_division z ON z.division = s.division
    WHERE NOT EXISTS (
        SELECT 1 FROM climate h
        WHERE h.zip3 = z.zip3 AND h.{clean} > 0
          AND h.month BETWEEN s.event_month - INTERVAL {before} MONTH
                          AND s.event_month + INTERVAL {after} MONTH
    )
)
SELECT
    strftime(u.event_month, '%Y-%m') AS stack,
    u.zip3,
    u.treated,
    d.division,
    strftime(p.period, '%Y-%m') AS period,
    date_diff('month', u.event_month, p.period) AS rel,
    p.n_loans,
    {counts}
FROM units u
JOIN zip3_division d USING (zip3)
JOIN panel p
  ON p.zip3 = u.zip3
 AND p.period BETWEEN u.event_month - INTERVAL {before} MONTH
                  AND u.event_month + INTERVAL {after} MONTH
WHERE p.n_loans > 0
"""


@dataclass
class EventStudyResult:
    outcome: str
    coefficients: pd.DataFrame  # rel, estimate, se, ci_low, ci_high (rel = -1 is 0 by definition)
    average_effect: float
    average_se: float
    pretrend_mean: float  # mean of the pre-event coefficients (k = -12 ... -2), pp
    pretrend_mean_p: float
    pretrend_slope: float  # OLS slope of the pre-event coefficients on k, pp per month
    pretrend_slope_p: float
    baseline_rate: float  # treated ZIP3, months -12 ... -1
    n_obs: int


def estimate(df: pd.DataFrame, outcome: str, numerator: str, cfg: dict) -> EventStudyResult:
    """Fit the stacked event study for one outcome (rate in % of loans)."""
    data = df.copy()
    data["y"] = 100.0 * data[numerator] / data["n_loans"]
    fit = pf.feols(
        "y ~ i(rel, treated, ref=-1) | zip3^stack + division^period^stack",
        data=data,
        weights="n_loans",
        vcov={"CRV1": "zip3"},
    )
    names = [str(n) for n in fit._coefnames]
    rel = np.array([int(n.split("::")[1].split(":")[0]) for n in names])
    beta = np.asarray(fit.coef(), dtype=float)
    vcov = np.asarray(fit._vcov, dtype=float)
    se = np.sqrt(np.diag(vcov))
    t_crit = stats.t.ppf(0.975, df=max(data["zip3"].nunique() - 1, 1))
    coefs = pd.DataFrame(
        {
            "rel": rel,
            "estimate": beta,
            "se": se,
            "ci_low": beta - t_crit * se,
            "ci_high": beta + t_crit * se,
        }
    )
    coefs = (
        pd.concat(
            [
                coefs,
                pd.DataFrame(
                    [{"rel": -1, "estimate": 0.0, "se": 0.0, "ci_low": 0.0, "ci_high": 0.0}]
                ),
            ]
        )
        .sort_values("rel")
        .reset_index(drop=True)
    )

    clusters = int(data["zip3"].nunique())

    def combination(w: np.ndarray) -> tuple[float, float, float]:
        """Estimate, standard error and two-sided p-value of w'b (t with G - 1 df)."""
        est, se_w = float(w @ beta), float(np.sqrt(w @ vcov @ w))
        p = float(2 * stats.t.sf(abs(est / se_w), max(clusters - 1, 1))) if se_w > 0 else 1.0
        return est, se_w, p

    # Average effect over k = first ... last.
    first, last = cfg["average_effect_months"]
    w = np.where((rel >= first) & (rel <= last), 1.0, 0.0)
    average, average_se, _ = combination(w / w.sum())

    # D013: test the mean and slope of the leads. Non-rejection does not establish
    # parallel trends; inspect all lead estimates and their uncertainty as well.
    pre = rel < -1
    pre_mean, _, pre_mean_p = combination(pre / pre.sum())
    x = np.where(pre, rel - rel[pre].mean(), 0.0)
    pre_slope, _, pre_slope_p = combination(x / (x @ x))

    base = data[(data["treated"] == 1) & (data["rel"] < 0)]
    baseline = float(np.average(base["y"], weights=base["n_loans"])) if len(base) else np.nan
    return EventStudyResult(
        outcome,
        coefs,
        average,
        average_se,
        pre_mean,
        pre_mean_p,
        pre_slope,
        pre_slope_p,
        baseline,
        int(fit._N),
    )


def design_summary(df: pd.DataFrame) -> dict:
    treated = df[df["treated"] == 1][["stack", "zip3"]].drop_duplicates()
    controls = df[df["treated"] == 0][["stack", "zip3"]].drop_duplicates()
    return {
        "stacks (event months)": df["stack"].nunique(),
        "treated ZIP3-events": len(treated),
        "distinct treated ZIP3": treated["zip3"].nunique(),
        "control ZIP3 per stack (median)": int(controls.groupby("stack").size().median()),
        "first event": df["stack"].min(),
        "last event": df["stack"].max(),
        "rows (stack x ZIP3 x month)": len(df),
    }


# --- Figures --------------------------------------------------------------------------

LABELS = {
    "dlq60": "Loans 60+ days past due",
    "dlq90": "Loans 90+ days past due",
    "disaster_flag": "Delinquency flagged 'due to disaster'",
    "forbearance": "Loans in forbearance",
    "prepaid": "Loans prepaid this month",
}


def _plot_coefficients(ax, coefs: pd.DataFrame) -> None:
    ax.axhline(0, color=COLORS["axis"], linewidth=0.8)
    ax.axvline(-0.5, color=COLORS["ink_secondary"], linewidth=0.8)
    ax.fill_between(
        coefs["rel"],
        coefs["ci_low"],
        coefs["ci_high"],
        color=COLORS["focus"],
        alpha=0.15,
        linewidth=0,
    )
    ax.plot(coefs["rel"], coefs["estimate"], color=COLORS["focus"], marker="o", markersize=3.5)


def fig_main(result: EventStudyResult, path: Path, cfg: dict) -> None:
    fig, ax = plt_subplots(figsize=(9, 4.6))
    _plot_coefficients(ax, result.coefficients)
    first, last = cfg["average_effect_months"]
    ax.set_title(
        f"Estimated change in 60+ day delinquency: {result.average_effect:+.2f} pp on average "
        f"in months {first}-{last}"
        if result.average_effect > 0
        else f"Average effect in months {first}-{last}: {result.average_effect:.2f} pp",
        fontsize=11,
    )
    ax.set_ylabel(f"Effect on {LABELS[result.outcome].lower()} (pp)")
    ax.set_xlabel("Months relative to the disaster (reference: month -1)")
    ax.text(
        0,
        -0.2,
        f"Stacked event study, ZIP3 x month, weighted by loans; 95% CI clustered by ZIP3. "
        f"Pre-event baseline {result.baseline_rate:.2f}%.\n"
        f"Pre-trends: mean of leads {result.pretrend_mean:+.3f} pp (p = "
        f"{result.pretrend_mean_p:.2f}), slope {result.pretrend_slope:+.4f} pp/month (p = "
        f"{result.pretrend_slope_p:.2f}). "
        "Source: Freddie Mac SFLLD, OpenFEMA.",
        transform=ax.transAxes,
        va="top",
        color=COLORS["muted"],
        fontsize=8,
    )
    save(fig, path)


def fig_outcomes(results: list[EventStudyResult], path: Path) -> None:
    n = len(results)
    cols = 2
    rows = (n + cols - 1) // cols
    fig, axes = plt_subplots(rows, cols, figsize=(9, 3.0 * rows), sharex=True)
    axes = np.atleast_1d(axes).ravel()
    for ax, res in zip(axes, results, strict=False):
        _plot_coefficients(ax, res.coefficients)
        ax.set_title(LABELS.get(res.outcome, res.outcome), fontsize=10)
        ax.set_ylabel("pp")
    for ax in axes[n:]:
        ax.set_visible(False)
    fig.suptitle(
        "Event-study estimates by outcome (95% CI clustered by ZIP3)",
        x=0.07,
        ha="left",
        fontweight="bold",
        fontsize=12,
    )
    fig.supxlabel("Months relative to the disaster", fontsize=10, color=COLORS["ink_secondary"])
    fig.tight_layout()
    save(fig, path)


def plt_subplots(*args, **kwargs):
    import matplotlib.pyplot as plt

    return plt.subplots(*args, **kwargs)


def save(fig, path: Path) -> None:
    import matplotlib.pyplot as plt

    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    log.info("Saved %s", path)


# --- Pipeline -------------------------------------------------------------------------


def build_stacked(con: duckdb.DuckDBPyConnection, cfg: dict, threshold: float) -> pd.DataFrame:
    return con.execute(stacked_query(cfg, threshold)).df()


def run(
    processed_dir: Path = PROCESSED_DIR,
    reports_dir: Path = REPORTS_DIR,
    cfg: dict | None = None,
) -> list[EventStudyResult]:
    cfg = cfg or load_config()
    apply_style()
    reports_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    register_views(con, processed_dir)

    threshold = float(cfg["treatment"]["hit_share_threshold"])
    df = build_stacked(con, cfg, threshold)
    if df.empty or df["treated"].sum() == 0:
        raise SystemExit("No clean events found: check the configuration")
    summary = design_summary(df)
    log.info("Design: %s", summary)

    results = [estimate(df, name, col, cfg) for name, col in cfg["outcomes"].items()]
    main = next(r for r in results if r.outcome == cfg["main_outcome"])

    robustness = []
    for t in cfg["robustness_thresholds"]:
        d = df if float(t) == threshold else build_stacked(con, cfg, float(t))
        if d.empty or d["treated"].sum() == 0:
            continue
        r = estimate(d, main.outcome, cfg["outcomes"][main.outcome], cfg)
        n_events = len(d[d["treated"] == 1][["stack", "zip3"]].drop_duplicates())
        robustness.append(
            (t, n_events, r.average_effect, r.average_se, r.pretrend_mean_p, r.pretrend_slope_p)
        )

    figures = reports_dir / "figures"
    fig_main(main, figures / "fig6_event_study_dlq60.png", cfg)
    fig_outcomes(results, figures / "fig7_event_study_outcomes.png")

    coefs = pd.concat([r.coefficients.assign(outcome=r.outcome) for r in results])
    coefs.to_csv(reports_dir / "event_study_coefficients.csv", index=False)

    first, last = cfg["average_effect_months"]
    fmt = {
        1: "{:.3f}",
        2: "{:.3f}",
        3: "{:.3f}",
        4: "{:.3f}",
        5: "{:.2f}",
        6: "{:.4f}",
        7: "{:.2f}",
    }
    sections = [
        "# Event study: climate disasters and mortgage delinquency",
        "Generated by `python -m climrisk.causal.event_study`. Stacked event study, "
        "ZIP3 x month, weighted by loans, 95% CI clustered by ZIP3.",
        "Interpretation requires parallel trends and no spillovers. Mean/slope lead tests "
        "do not test every possible pre-trend; non-rejection is not proof of validity. "
        "Grace-period hits need not belong to the same disaster. Repeated controls and "
        "loan weights determine the pooled estimand (D012-D013).",
        "## Design",
        md_table(["item", "value"], list(summary.items())),
        "## Main result",
        "![Event study](figures/fig6_event_study_dlq60.png)",
        "## Summary by outcome",
        md_table(
            [
                "outcome",
                f"avg_effect_pp_k{first}_{last}",
                "se",
                "baseline_pct",
                "pre_mean_pp",
                "pre_mean_p",
                "pre_slope_pp_month",
                "pre_slope_p",
                "n_obs",
            ],
            [
                (
                    r.outcome,
                    r.average_effect,
                    r.average_se,
                    r.baseline_rate,
                    r.pretrend_mean,
                    r.pretrend_mean_p,
                    r.pretrend_slope,
                    r.pretrend_slope_p,
                    r.n_obs,
                )
                for r in results
            ],
            fmt,
        ),
        "![Outcomes](figures/fig7_event_study_outcomes.png)",
        f"## Robustness: treatment threshold ({main.outcome})",
        md_table(
            [
                "threshold",
                "treated_events",
                f"avg_effect_pp_k{first}_{last}",
                "se",
                "pre_mean_p",
                "pre_slope_p",
            ],
            robustness,
            {2: "{:.3f}", 3: "{:.3f}", 4: "{:.2f}", 5: "{:.2f}"},
        ),
        f"## Coefficients ({main.outcome})",
        md_table(
            ["rel_month", "estimate_pp", "se", "ci_low", "ci_high"],
            list(main.coefficients.itertuples(index=False, name=None)),
            {1: "{:.3f}", 2: "{:.3f}", 3: "{:.3f}", 4: "{:.3f}"},
        ),
    ]
    report = reports_dir / "event_study.md"
    report.write_text("\n\n".join(sections) + "\n", encoding="utf-8")
    log.info("Wrote %s", report)
    con.close()
    return results


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()


if __name__ == "__main__":
    main()
