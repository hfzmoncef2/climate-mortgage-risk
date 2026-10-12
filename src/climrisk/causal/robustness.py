"""Event-study robustness and cure analysis (end of Phase 5).

1. Heterogeneity by disaster type (hurricane / flood / fire).
2. Period split: events before vs from 2010 (pre-2010 pre-periods overlap the crisis).
3. Cure analysis: after a loan's first 90+ day delinquency (our default definition, D004),
   how often does it end in a loss, return to current, or stay delinquent? Split by whether
   the delinquency was flagged "due to disaster" or in forbearance.

Outputs:
    reports/event_study_robustness.md
    reports/figures/fig8_event_study_by_type.png

Usage:
    uv run --frozen python -m climrisk.causal.robustness
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import yaml

from climrisk.causal import event_study as es
from climrisk.features import climate, target
from climrisk.features.target import INTERIM_DIR, PROCESSED_DIR, sql_codes
from climrisk.ingest.freddie import ROOT, _sql_str
from climrisk.viz.eda import apply_style, md_table

CONFIG_PATH = ROOT / "configs" / "robustness.yaml"

log = logging.getLogger(__name__)


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# --- 1. Disaster type of each treated ZIP3-event --------------------------------------


def event_types_query(climate_cfg: dict, rob_cfg: dict, fema_path: Path) -> str:
    """(zip3, stack, incident_group): the group holding most affected homes that month."""
    cases = " ".join(
        f"WHEN incident_type IN ({sql_codes(types)}) THEN {_sql_str(group)}"
        for group, types in rob_cfg["incident_groups"].items()
    )
    return f"""
WITH ev AS ({climate.events_query(climate_cfg, "main", fema_path)}),
types AS (
    SELECT DISTINCT disaster_number, CASE {cases} ELSE 'Other' END AS incident_group
    FROM read_parquet({_sql_str(fema_path)})
),
county_groups AS (
    SELECT DISTINCT ev.county_fips, ev.month, t.incident_group
    FROM ev JOIN types t USING (disaster_number)
),
by_group AS (
    SELECT w.zip3, strftime(ev.month, '%Y-%m') AS stack, ev.incident_group,
           sum(w.weight) AS share
    FROM county_groups ev
    JOIN weights w USING (county_fips)
    GROUP BY ALL
)
SELECT zip3, stack, incident_group FROM by_group
QUALIFY row_number() OVER (PARTITION BY zip3, stack
                           ORDER BY share DESC, incident_group) = 1
"""


def subset(df: pd.DataFrame, keep_events: pd.DataFrame) -> pd.DataFrame:
    """Keep the treated ZIP3-events in `keep_events` (zip3, stack) and the controls of the
    stacks that still contain at least one treated event."""
    keys = set(map(tuple, keep_events[["zip3", "stack"]].to_numpy()))
    in_keys = np.array([(z, s) in keys for z, s in zip(df["zip3"], df["stack"], strict=True)])
    is_kept_treated = (df["treated"] == 1).to_numpy() & in_keys
    stacks = set(df.loc[is_kept_treated, "stack"])
    return df[is_kept_treated | ((df["treated"] == 0) & df["stack"].isin(stacks))]


def summarise(name: str, d: pd.DataFrame, es_cfg: dict, min_events: int = 10) -> tuple | None:
    treated = d.loc[d["treated"] == 1, ["zip3", "stack"]].drop_duplicates()
    if len(treated) < min_events:  # too few events for a clustered estimate
        return None
    main = es_cfg["main_outcome"]
    r = es.estimate(d, main, es_cfg["outcomes"][main], es_cfg)
    return (
        name,
        len(treated),
        r.average_effect,
        r.average_se,
        r.baseline_rate,
        r.pretrend_mean_p,
        r.pretrend_slope_p,
        r,
    )


# --- 3. Cure analysis -----------------------------------------------------------------


def cure_query(target_cfg: dict, rob_cfg: dict, loans_path: Path) -> str:
    c = rob_cfg["cure"]
    h, lookback = int(c["horizon_months"]), int(c["flag_lookback_months"])
    cutoff = date.fromisoformat(target_cfg["performance_cutoff"])
    covid_start = date.fromisoformat(target_cfg["covid_window"]["start"])
    covid_end = date.fromisoformat(target_cfg["covid_window"]["end"])
    loss = sql_codes(target_cfg["loss_zero_balance_codes"])
    prepay = sql_codes(target_cfg["prepay_zero_balance_codes"])
    transfer = sql_codes(target_cfg["censor_zero_balance_codes"])
    reo = sql_codes(target_cfg["default_status_codes"])
    return f"""
WITH defaults AS (
    -- first default of each loan, observable for the full horizon, outside COVID
    SELECT loan_id, CAST(default_date AS DATE) AS default_date
    FROM read_parquet({_sql_str(loans_path)})
    WHERE outcome = 'default'
      AND default_date >= DATE '{c["first_default"]}'
      AND default_date + INTERVAL {h} MONTH <= DATE '{cutoff}'
      AND NOT (default_date BETWEEN DATE '{covid_start}' AND DATE '{covid_end}')
),
flags AS (
    SELECT d.loan_id,
           bool_or(p.disaster_delinquency = 'Y') AS disaster,
           bool_or(p.assistance_plan = 'F') AS forbearance
    FROM defaults d
    JOIN perf p ON p.loan_id = d.loan_id
     AND p.period BETWEEN d.default_date - INTERVAL {lookback} MONTH AND d.default_date
    GROUP BY d.loan_id
),
after AS (
    SELECT d.loan_id,
           bool_or((coalesce(p.zero_balance_date, p.period)
                     BETWEEN d.default_date AND d.default_date + INTERVAL {h} MONTH
                     AND p.zero_balance_code IN ({loss}))
                   OR (p.period BETWEEN d.default_date
                                     AND d.default_date + INTERVAL {h} MONTH
                       AND p.delinquency_status IN ({reo}))) AS loss,
           bool_or(p.period > d.default_date
                   AND p.period <= d.default_date + INTERVAL {h} MONTH
                   AND p.delinquency_status IN ('00', '0')) AS current_again,
           bool_or(coalesce(p.zero_balance_date, p.period)
                   BETWEEN d.default_date AND d.default_date + INTERVAL {h} MONTH
                   AND p.zero_balance_code IN ({prepay})) AS prepaid,
           bool_or(coalesce(p.zero_balance_date, p.period)
                   BETWEEN d.default_date AND d.default_date + INTERVAL {h} MONTH
                   AND p.zero_balance_code IN ({transfer})) AS transferred,
           bool_or(p.period = d.default_date + INTERVAL {h} MONTH
                   AND p.delinquency_status IN ('00', '0')) AS current_at_end,
           max(p.period) FILTER (WHERE p.period <= d.default_date + INTERVAL {h} MONTH)
               AS last_period,
           arg_max(p.delinquency_status, p.period)
               FILTER (WHERE p.period <= d.default_date + INTERVAL {h} MONTH) AS last_status
    FROM defaults d
    JOIN perf p ON p.loan_id = d.loan_id AND p.period <= DATE '{cutoff}'
               AND p.period >= d.default_date
    GROUP BY d.loan_id
),
outcomes AS (
    SELECT
        CASE WHEN coalesce(f.disaster, false) THEN 'Flagged due to disaster'
             WHEN coalesce(f.forbearance, false) THEN 'Forbearance, not disaster'
             ELSE 'Neither flag' END AS default_type,
        coalesce(a.current_at_end, false)
            AND NOT coalesce(a.loss OR a.prepaid OR a.transferred, false) AS current_at_end,
        CASE WHEN a.loss THEN 'loss'
             WHEN a.current_again OR a.prepaid THEN 'reperforming'
             WHEN a.transferred THEN 'transferred'
             WHEN a.last_period >= d.default_date + INTERVAL {h} MONTH
                  AND (TRY_CAST(a.last_status AS INTEGER) >= 1
                       OR a.last_status IN ({reo}))
                 THEN 'still_delinquent'
             ELSE 'unknown_followup' END AS outcome
    FROM defaults d
    LEFT JOIN flags f USING (loan_id)
    LEFT JOIN after a USING (loan_id)
)
SELECT default_type,
       count(*) AS defaults,
       100.0 * avg((outcome = 'loss')::INT) AS loss_pct,
       100.0 * avg((outcome = 'reperforming')::INT) AS reperforming_pct,
       100.0 * avg((outcome = 'still_delinquent')::INT) AS still_delinquent_pct,
       100.0 * avg((outcome = 'transferred')::INT) AS transferred_pct,
       100.0 * avg((outcome = 'unknown_followup')::INT) AS unknown_followup_pct,
       100.0 * avg(current_at_end::INT) AS current_at_end_pct
FROM outcomes GROUP BY default_type ORDER BY defaults DESC
"""


# --- Figure ---------------------------------------------------------------------------


def fig_by_type(results: list[tuple], path: Path) -> None:
    fig, axes = es.plt_subplots(1, len(results), figsize=(9, 3.4), sharey=True)
    axes = np.atleast_1d(axes)
    for ax, row in zip(axes, results, strict=True):
        name, n, avg, se = row[0], row[1], row[2], row[3]
        es._plot_coefficients(ax, row[7].coefficients)
        ax.set_title(f"{name}\n{n} events, avg {avg:+.2f} pp (se {se:.2f})", fontsize=9.5)
        ax.set_xlabel("Months from disaster", fontsize=9)
    axes[0].set_ylabel("Effect on 60+ dpd (pp)")
    fig.suptitle(
        "Effect on 60+ day delinquency by disaster type (95% CI clustered by ZIP3)",
        x=0.07,
        ha="left",
        fontweight="bold",
        fontsize=11.5,
    )
    fig.tight_layout()
    es.save(fig, path)


# --- Pipeline -------------------------------------------------------------------------


def run(
    processed_dir: Path = PROCESSED_DIR,
    interim_dir: Path = INTERIM_DIR,
    fema_path: Path = climate.FEMA_PATH,
    reports_dir: Path = es.REPORTS_DIR,
    es_cfg: dict | None = None,
    rob_cfg: dict | None = None,
    climate_cfg: dict | None = None,
    target_cfg: dict | None = None,
) -> dict:
    es_cfg = es_cfg or es.load_config()
    rob_cfg = rob_cfg or load_config()
    climate_cfg = climate_cfg or climate.load_config()
    target_cfg = target_cfg or target.load_config()
    apply_style()

    reports_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    es.register_views(con, processed_dir)
    df = es.build_stacked(con, es_cfg, float(es_cfg["treatment"]["hit_share_threshold"]))
    events = df.loc[df["treated"] == 1, ["zip3", "stack"]].drop_duplicates()
    min_events = int(rob_cfg.get("min_events", 10))

    # 1. By type
    types = con.execute(event_types_query(climate_cfg, rob_cfg, fema_path)).df()
    typed = events.merge(types, on=["zip3", "stack"], how="left")
    by_type = []
    for group in list(rob_cfg["incident_groups"]) + ["Other"]:
        row = summarise(
            group, subset(df, typed[typed["incident_group"] == group]), es_cfg, min_events
        )
        if row:
            by_type.append(row)

    # 2. By period
    split = rob_cfg["period_split"]
    by_period = [
        r
        for r in (
            summarise("All events", df, es_cfg, min_events),
            summarise(
                f"Before {split}", subset(df, events[events["stack"] < split]), es_cfg, min_events
            ),
            summarise(
                f"From {split}", subset(df, events[events["stack"] >= split]), es_cfg, min_events
            ),
        )
        if r
    ]

    # 3. Cure analysis
    target.register_views(con, interim_dir)
    cure = con.execute(cure_query(target_cfg, rob_cfg, processed_dir / "loans.parquet"))
    cure_rows = cure.fetchall()

    if by_type:
        fig_by_type(by_type, reports_dir / "figures" / "fig8_event_study_by_type.png")

    first, last = es_cfg["average_effect_months"]
    headers = [
        "subsample",
        "treated_events",
        f"avg_effect_pp_k{first}_{last}",
        "se",
        "baseline_pct",
        "pre_mean_p",
        "pre_slope_p",
    ]
    fmt = {2: "{:.3f}", 3: "{:.3f}", 4: "{:.3f}", 5: "{:.2f}", 6: "{:.2f}"}
    c = rob_cfg["cure"]
    sections = [
        "# Event study robustness and cure analysis",
        "Generated by `python -m climrisk.causal.robustness`. Same design as "
        "`reports/event_study.md` (main outcome: 60+ day delinquency).",
        "## 1. By disaster type",
        "![By type](figures/fig8_event_study_by_type.png)" if by_type else "",
        md_table(headers, [r[:7] for r in by_type], fmt),
        f"## 2. By period (split at {split})",
        md_table(headers, [r[:7] for r in by_period], fmt),
        "## 3. What happens after a default?",
        f"Loans labelled default by D004 (within the first 24 months from first payment), "
        f"whose first qualifying default occurred from {c['first_default']}, "
        f"outside COVID, followed for {c['horizon_months']} months. Flags are taken in the "
        f"default month or the {c['flag_lookback_months']} months before. 'loss' = loss exit "
        "or REO (not necessarily a realized accounting loss); exits use zero_balance_date "
        "with reporting month as fallback. Follow-up runs through default date +24 months. "
        "'reperforming' = ever back to current or prepaid, provided no loss was observed. "
        "'current_at_end_pct' uses all defaults as its denominator and requires an observed "
        "current status exactly at +24 months, with no loss/REO, prepayment or transfer "
        "in follow-up. It is a separate measure, not another mutually exclusive outcome. "
        "Missing endpoint records and paid-off loans do not count as current. "
        "Defaults during COVID are excluded; follow-up windows may overlap COVID. "
        "This is not a lifetime-default cohort. Missing terminal follow-up is unknown, "
        "not evidence of continuing delinquency. Loss takes precedence over any cure; "
        "these are descriptive outcomes, not causal comparisons of assistance programs.",
        md_table(
            [
                "default_type",
                "defaults",
                "loss_pct",
                "reperforming_pct",
                "still_delinquent_pct",
                "transferred_pct",
                "unknown_followup_pct",
                "current_at_end_pct",
            ],
            cure_rows,
            {2: "{:.1f}", 3: "{:.1f}", 4: "{:.1f}", 5: "{:.1f}", 6: "{:.1f}", 7: "{:.1f}"},
        ),
    ]
    report = reports_dir / "event_study_robustness.md"
    report.write_text("\n\n".join(s for s in sections if s) + "\n", encoding="utf-8")
    log.info("Wrote %s", report)
    con.close()
    return {"by_type": by_type, "by_period": by_period, "cure": cure_rows}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()


if __name__ == "__main__":
    main()
