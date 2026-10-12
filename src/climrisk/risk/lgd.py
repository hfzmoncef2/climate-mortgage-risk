"""Loss given default (LGD) of D004 defaults, measured in the data (Phase 8).

For every D004 default (first default month D) with a complete follow-up (D + 60 months
<= performance cutoff, whatever the outcome):
    EAD          unpaid balance in month D (Freddie Mac shows 0 in the first months of a
                 loan: the original balance is used then)
    resolution   within (D, D+60]:
                     loss exit (02, 03, 09)   loss = actual_loss reported by Freddie Mac
                                              (= balance + accrued interest + expenses
                                                 - sale proceeds - recoveries)
                     voluntary payoff (01)    loss = 0
                     no exit, current at the last month observed in the follow-up -> 0
                     loss exit with no published amount -> EAD x severity of the
                                              reported loss exits of the same period
                     transfer (15, 16, 96) or still delinquent: unresolved
    LGD          sum(loss) / sum(EAD) over resolved defaults (exposure-weighted), with
                 bounds over all defaults: unresolved = no loss (low) or a loss exit at
                 the period's severity (high)
Modification costs (cumulative, Freddie Mac) are reported next to it, not added.
Breakdowns: default period, origination LTV, and, from 2014 (when the flag exists),
disaster-flagged vs forbearance vs other defaults.

Outputs:
    data/processed/lgd_defaults.parquet
    reports/lgd.md

Usage:
    uv run --frozen python -m climrisk.risk.lgd
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import duckdb
import yaml

from climrisk.causal import event_study as es
from climrisk.features import target
from climrisk.features.target import INTERIM_DIR, PROCESSED_DIR, sql_codes
from climrisk.ingest.freddie import ROOT
from climrisk.viz.eda import md_table

CONFIG_PATH = ROOT / "configs" / "stress.yaml"

log = logging.getLogger(__name__)


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def lgd_query(cfg: dict, target_cfg: dict, loans_path: Path) -> str:
    c = cfg["lgd"]
    h, back = int(c["follow_up_months"]), int(c["flag_lookback_months"])
    cutoff = date.fromisoformat(target_cfg["performance_cutoff"])
    loss = sql_codes(target_cfg["loss_zero_balance_codes"])
    prepay = sql_codes(target_cfg["prepay_zero_balance_codes"])
    periods = " ".join(
        f"WHEN year(d.dd) BETWEEN {lo} AND {hi} THEN '{name}'"
        for name, (lo, hi) in c["default_year_groups"].items()
    )
    edges = [float(x) for x in c["ltv_bins"]]
    ltv_case = " ".join(
        f"WHEN d.ltv >= {lo:g} AND d.ltv < {hi:g} THEN "
        + (f"'{lo:g}+'" if hi == edges[-1] else f"'{lo:g}-{hi:g}'")
        for lo, hi in zip(edges[:-1], edges[1:], strict=True)
    )
    return f"""
WITH d AS (
    SELECT loan_id, vintage, ltv, orig_upb, exit_code,
           CAST(default_date AS DATE) AS dd, CAST(exit_date AS DATE) AS exit_date
    FROM read_parquet('{loans_path}')
    WHERE in_pd_sample AND default_24m = 1
      AND CAST(default_date AS DATE) + INTERVAL {h} MONTH <= DATE '{cutoff}'
),
at_default AS (
    SELECT d.loan_id,
           arg_max(p.current_upb, p.period) FILTER (WHERE p.period <= d.dd) AS upb,
           bool_or(p.disaster_delinquency = 'Y')
               FILTER (WHERE p.period >= d.dd - INTERVAL {back} MONTH AND p.period <= d.dd)
               AS disaster,
           bool_or(p.assistance_plan = 'F')
               FILTER (WHERE p.period >= d.dd - INTERVAL {back} MONTH AND p.period <= d.dd)
               AS forbearance
    FROM d JOIN perf p ON p.loan_id = d.loan_id AND p.period <= d.dd
    GROUP BY d.loan_id
),
follow AS (
    SELECT d.loan_id,
           arg_max(p.delinquency_status, p.period) AS last_status,
           max(p.cum_modification_cost) AS mod_cost
    FROM d JOIN perf p ON p.loan_id = d.loan_id
     AND p.period > d.dd AND p.period <= d.dd + INTERVAL {h} MONTH
    GROUP BY d.loan_id
),
exit_row AS (
    -- the loss is reported on the zero-balance record, whatever its reporting month
    SELECT p.loan_id,
           arg_max(p.actual_loss, p.period) FILTER (WHERE p.zero_balance_code IS NOT NULL)
               AS actual_loss
    FROM perf p SEMI JOIN d ON p.loan_id = d.loan_id
    GROUP BY p.loan_id
),
resolved AS (
    SELECT d.loan_id, d.vintage, d.dd AS default_month,
           CASE {periods} ELSE 'other' END AS default_period,
           CASE {ltv_case} ELSE 'missing' END AS ltv_group,
           CASE WHEN d.dd < DATE '{c["flag_era_start"]}' THEN NULL
                WHEN coalesce(a.disaster, false) THEN 'disaster-flagged'
                WHEN coalesce(a.forbearance, false) THEN 'forbearance, not disaster'
                ELSE 'neither flag' END AS default_type,
           coalesce(nullif(a.upb, 0), d.orig_upb) AS ead,
           coalesce(f.mod_cost, 0) AS mod_cost,
           CASE
               WHEN d.exit_code IN ({loss}) AND d.exit_date <= d.dd + INTERVAL {h} MONTH
                   THEN CASE WHEN x.actual_loss IS NULL THEN 'loss_unreported' ELSE 'loss' END
               WHEN d.exit_code IN ({prepay}) AND d.exit_date <= d.dd + INTERVAL {h} MONTH
                   THEN 'paid_off'
               WHEN d.exit_code IS NOT NULL AND d.exit_date <= d.dd + INTERVAL {h} MONTH
                   THEN 'transferred'
               WHEN f.last_status = '00' THEN 'cured'
               ELSE 'still_delinquent'
           END AS resolution,
           x.actual_loss
    FROM d
    LEFT JOIN at_default a USING (loan_id)
    LEFT JOIN follow f USING (loan_id)
    LEFT JOIN exit_row x USING (loan_id)
),
severity AS (
    -- loss / EAD of the reported loss exits, by default period
    SELECT default_period, sum(actual_loss) / sum(ead) AS period_severity
    FROM resolved WHERE resolution = 'loss' GROUP BY 1
),
losses AS (
    SELECT r.*, s.period_severity,
           r.resolution IN ('loss', 'loss_unreported', 'paid_off', 'cured') AS lgd_resolved,
           CASE r.resolution
               WHEN 'loss' THEN r.actual_loss
               -- a loss exit whose amount is not published: the period's severity
               WHEN 'loss_unreported' THEN r.ead * s.period_severity
               WHEN 'paid_off' THEN 0
               WHEN 'cured' THEN 0
           END AS loss
    FROM resolved r LEFT JOIN severity s USING (default_period)
)
SELECT *,
       coalesce(loss, 0) AS loss_low,                        -- unresolved: no loss
       coalesce(loss, ead * period_severity) AS loss_high    -- unresolved: a loss exit
FROM losses
ORDER BY loan_id
"""


def group_rows(con: duckdb.DuckDBPyConnection, path: Path, column: str) -> list[tuple]:
    return con.execute(f"""
        SELECT {column},
               count(*) AS defaults,
               100 * avg(lgd_resolved::INT) AS resolved_pct,
               100 * avg((resolution IN ('loss', 'loss_unreported'))::INT)
                   FILTER (WHERE lgd_resolved) AS loss_exit_pct,
               100 * sum(loss) FILTER (WHERE resolution = 'loss')
                   / sum(ead) FILTER (WHERE resolution = 'loss') AS severity_pct,
               100 * sum(loss_low) / sum(ead) AS lgd_low_pct,
               100 * sum(loss) FILTER (WHERE lgd_resolved)
                   / sum(ead) FILTER (WHERE lgd_resolved) AS lgd_pct,
               100 * sum(loss_high) / sum(ead) AS lgd_high_pct,
               100 * sum(mod_cost) FILTER (WHERE lgd_resolved)
                   / sum(ead) FILTER (WHERE lgd_resolved) AS mod_cost_pct
        FROM read_parquet('{path}')
        WHERE {column} IS NOT NULL
        GROUP BY 1 ORDER BY 1
    """).fetchall()


def run(
    processed_dir: Path = PROCESSED_DIR,
    interim_dir: Path = INTERIM_DIR,
    reports_dir: Path = es.REPORTS_DIR,
    cfg: dict | None = None,
    target_cfg: dict | None = None,
) -> Path:
    cfg = cfg or load_config()
    target_cfg = target_cfg or target.load_config()
    con = duckdb.connect()
    target.register_views(con, interim_dir)
    out = processed_dir / "lgd_defaults.parquet"
    tmp = out.with_name(out.name + ".tmp")
    query = lgd_query(cfg, target_cfg, processed_dir / "loans.parquet")
    con.execute(f"COPY ({query}) TO '{tmp}' (FORMAT PARQUET)")
    tmp.replace(out)

    resolutions = con.execute(
        f"SELECT resolution, count(*) FROM read_parquet('{out}') GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()
    log.info("Defaults by resolution: %s", dict(resolutions))
    headers = [
        "defaults",
        "resolved_pct",
        "loss_exit_pct",
        "severity_pct",
        "lgd_low_pct",
        "lgd_pct",
        "lgd_high_pct",
        "mod_cost_pct",
    ]
    fmt = {
        2: "{:.1f}",
        3: "{:.1f}",
        4: "{:.1f}",
        5: "{:.2f}",
        6: "{:.2f}",
        7: "{:.2f}",
        8: "{:.2f}",
    }
    h = cfg["lgd"]["follow_up_months"]
    sections = [
        "# Loss given default (LGD) of D004 defaults",
        "Generated by `python -m climrisk.risk.lgd`. D004 defaults of the PD sample with a "
        f"complete {h}-month follow-up after the default month. EAD = balance at default. "
        "Loss = Freddie Mac actual loss for loss exits (the period's severity when the amount "
        "is not published), 0 for payoffs and loans current at the end of the follow-up. "
        "Transfers and loans still delinquent are unresolved: their loss is unknown.",
        "Columns: `resolved_pct` share of defaults with a known (or imputed) loss; "
        "`loss_exit_pct` share of resolved defaults ending in a loss exit; `severity_pct` "
        "loss / EAD of the reported loss exits; `lgd_pct` loss / EAD over resolved defaults; "
        "`lgd_low_pct` and `lgd_high_pct` bound it over all defaults, counting every "
        "unresolved default as no loss (low) or as a loss exit at the period's severity "
        "(high); `mod_cost_pct` cumulative modification costs / EAD, reported separately.",
        "## Resolution of the defaults",
        md_table(["resolution", "defaults"], resolutions),
        "## By default period",
        md_table(["default_period"] + headers, group_rows(con, out, "default_period"), fmt),
        "## By origination LTV",
        md_table(["ltv_group"] + headers, group_rows(con, out, "ltv_group"), fmt),
        f"## By default type (defaults from {cfg['lgd']['flag_era_start'][:4]})",
        md_table(["default_type"] + headers, group_rows(con, out, "default_type"), fmt),
    ]
    (reports_dir / "lgd.md").write_text("\n\n".join(sections) + "\n", encoding="utf-8")
    log.info("Wrote %s", reports_dir / "lgd.md")
    return out


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()


if __name__ == "__main__":
    main()
