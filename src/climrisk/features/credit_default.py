"""Credit-default sensitivity target (D015, D022; Phase 7).

D004 counts every 90+ day delinquency as a default, including temporary episodes that
cure. This variant keeps D004 as it is and looks at each D004 default 12 months after its
first default month D:
    temporary episode (0)   no loss exit / REO in [D, D+12], and either the loan is observed
                            current ("00") at D+12 or it is paid off voluntarily in (D, D+12]
    credit default (1)      everything else: loss exit or REO, still delinquent, transferred,
                            or not observed at D+12 (conservative)
Loans without a D004 default stay 0. Eligibility, for every loan whether it defaulted or
not: PD sample and window end + follow-up <= performance cutoff (a rule applied only to
defaults would bias the rate downwards).

Outputs:
    data/processed/credit_default.parquet   (loan_id, credit_eligible, resolution, workout,
                                             credit_default_24m)
    reports/credit_default.md               D004 vs credit-default rates by vintage

Usage:
    uv run --frozen python -m climrisk.features.credit_default
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import duckdb

from climrisk.causal import event_study as es
from climrisk.features import pd_features, target
from climrisk.features.target import INTERIM_DIR, PROCESSED_DIR, sql_codes
from climrisk.viz.eda import md_table

TEMPORARY = ("no_default", "temporary_current", "temporary_payoff")

log = logging.getLogger(__name__)


def credit_query(target_cfg: dict, follow_up: int, loans_path: Path) -> str:
    cutoff = date.fromisoformat(target_cfg["performance_cutoff"])
    loss = sql_codes(target_cfg["loss_zero_balance_codes"])
    prepay = sql_codes(target_cfg["prepay_zero_balance_codes"])
    reo = sql_codes(target_cfg["default_status_codes"])
    temporary = sql_codes(list(TEMPORARY))
    return f"""
WITH d AS (
    SELECT loan_id, vintage, default_24m, window_end, exit_code,
           CAST(default_date AS DATE) AS dd,
           CAST(exit_date AS DATE) AS exit_date
    FROM read_parquet('{loans_path}')
    WHERE in_pd_sample
),
path AS (
    -- statuses from the default month to the end of the follow-up, defaults only
    SELECT d.loan_id,
           bool_or(p.delinquency_status IN ({reo})) AS reo,
           bool_or(p.period = d.dd + INTERVAL {follow_up} MONTH
                   AND p.delinquency_status = '00') AS current_at_end,
           -- servicer workout during the follow-up (description only, not in the target)
           bool_or(p.modification_flag IN ('Y', 'P') OR p.payment_deferral IN ('Y', 'P'))
               AS workout
    FROM d
    JOIN perf p ON p.loan_id = d.loan_id
     AND p.period >= d.dd AND p.period <= d.dd + INTERVAL {follow_up} MONTH
    WHERE d.default_24m = 1
    GROUP BY d.loan_id
),
resolved AS (
    SELECT d.loan_id, d.vintage, d.default_24m,
           CAST(d.window_end + INTERVAL {follow_up} MONTH AS DATE) <= DATE '{cutoff}'
               AS credit_eligible,
           CASE
               WHEN d.default_24m = 0 THEN 'no_default'
               WHEN coalesce(pa.reo, false)
                 OR (d.exit_code IN ({loss})
                     AND d.exit_date <= d.dd + INTERVAL {follow_up} MONTH) THEN 'loss_or_reo'
               WHEN d.exit_code IN ({prepay}) AND d.exit_date > d.dd
                AND d.exit_date <= d.dd + INTERVAL {follow_up} MONTH THEN 'temporary_payoff'
               WHEN coalesce(pa.current_at_end, false) THEN 'temporary_current'
               ELSE 'persistent'
           END AS resolution,
           coalesce(pa.workout, false) AS workout
    FROM d
    LEFT JOIN path pa USING (loan_id)
)
SELECT *, CASE WHEN resolution IN ({temporary}) THEN 0 ELSE 1 END::TINYINT
              AS credit_default_24m
FROM resolved
ORDER BY loan_id
"""


def summary_rows(con: duckdb.DuckDBPyConnection, path: Path) -> list[tuple]:
    return con.execute(f"""
        SELECT vintage,
               count(*) AS loans,
               100 * avg(default_24m) AS d004_pct,
               100 * avg(credit_default_24m) AS credit_pct,
               count(*) FILTER (WHERE default_24m = 1) AS d004_defaults,
               100 * avg((resolution = 'loss_or_reo')::INT) FILTER (WHERE default_24m = 1),
               100 * avg((resolution = 'persistent')::INT) FILTER (WHERE default_24m = 1),
               100 * avg((resolution = 'temporary_current')::INT) FILTER (WHERE default_24m = 1),
               100 * avg((resolution = 'temporary_payoff')::INT) FILTER (WHERE default_24m = 1),
               100 * avg(workout::INT) FILTER (WHERE resolution = 'temporary_current')
        FROM read_parquet('{path}')
        WHERE credit_eligible
        GROUP BY vintage ORDER BY vintage
    """).fetchall()


def run(
    processed_dir: Path = PROCESSED_DIR,
    interim_dir: Path = INTERIM_DIR,
    reports_dir: Path = es.REPORTS_DIR,
    cfg: dict | None = None,
    target_cfg: dict | None = None,
) -> Path:
    cfg = cfg or pd_features.load_config()
    target_cfg = target_cfg or target.load_config()
    follow_up = int(cfg["credit_default"]["follow_up_months"])
    con = duckdb.connect()
    target.register_views(con, interim_dir)
    out = processed_dir / "credit_default.parquet"
    tmp = out.with_name(out.name + ".tmp")
    query = credit_query(target_cfg, follow_up, processed_dir / "loans.parquet")
    con.execute(f"COPY ({query}) TO '{tmp}' (FORMAT PARQUET)")
    tmp.replace(out)
    n, eligible = con.execute(
        f"SELECT count(*), count(*) FILTER (WHERE credit_eligible) FROM read_parquet('{out}')"
    ).fetchone()
    log.info("Wrote %s: %s PD-sample loans, %s eligible", out, f"{n:,}", f"{eligible:,}")

    rows = summary_rows(con, out)
    sections = [
        "# Credit-default sensitivity target (D022)",
        "Generated by `python -m climrisk.features.credit_default`. Each D004 default is "
        f"followed for {follow_up} months after its first default month D. Temporary "
        "episode: no loss exit or REO, and current at D+12 or paid off within the follow-up. "
        "Everything else stays a default. Eligible loans only (window end + follow-up <= "
        "performance cutoff), the same rule for every loan.",
        "## Default rates by vintage",
        "The four outcome columns split the D004 defaults (they sum to 100). The last "
        "column describes the loans current at D+12: the share with a loan modification "
        "or payment deferral during the follow-up (a servicer workout rather than a "
        "spontaneous cure; description only, the target is unchanged).",
        md_table(
            [
                "vintage",
                "loans",
                "d004_pct",
                "credit_pct",
                "d004_defaults",
                "loss_or_reo_pct",
                "persistent_pct",
                "current_at_D+12_pct",
                "paid_off_pct",
                "workout_among_current_pct",
            ],
            rows,
            {
                2: "{:.2f}",
                3: "{:.2f}",
                5: "{:.1f}",
                6: "{:.1f}",
                7: "{:.1f}",
                8: "{:.1f}",
                9: "{:.1f}",
            },
        ),
    ]
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "credit_default.md").write_text("\n\n".join(sections) + "\n", encoding="utf-8")
    log.info("Wrote %s", reports_dir / "credit_default.md")
    return out


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()


if __name__ == "__main__":
    main()
