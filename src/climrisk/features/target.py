"""Loan-level default target (Phase 2).

Builds data/processed/loans.parquet: one row per loan with origination features and a
24-month default label, following configs/target.yaml (DECISIONS.md D004-D005).

Label (`default_24m`) within the first `horizon_months` after the first payment date:
    1    -> 90+ days past due or REO acquisition (RA) in a window month, or a loss exit
            (zero balance 02/03/09) dated inside the window
    0    -> voluntary payoff dated inside the window before any default, or no default
            and the loan still observed at or after the window end (gaps allowed)
    NULL -> censored: window beyond the data cutoff, transfer out (15/16/96) inside the
            window, or history ending before the window end without an exit code

Exits are dated with zero_balance_date, not with the reporting month of the exit row.

`in_pd_sample` marks the loans used for PD modelling (labelled, non-HARP, no date
anomaly, window not overlapping COVID, known ZIP3).

Usage:
    uv run --frozen python -m climrisk.features.target
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import duckdb
import yaml

from climrisk.ingest.freddie import OUT_DIR as INTERIM_DIR
from climrisk.ingest.freddie import ROOT, _sql_str

CONFIG_PATH = ROOT / "configs" / "target.yaml"
PROCESSED_DIR = ROOT / "data" / "processed"
REPORT_PATH = ROOT / "reports" / "target_summary.md"

log = logging.getLogger(__name__)


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def register_views(con: duckdb.DuckDBPyConnection, data_dir: Path = INTERIM_DIR) -> None:
    """Expose the interim Parquet files as two SQL views: orig and perf."""
    for view, table in (("orig", "origination"), ("perf", "performance")):
        files = sorted((data_dir / table).glob("*.parquet"))
        if not files:
            raise FileNotFoundError(f"No Parquet files in {data_dir / table}: run `make data`")
        file_list = ", ".join(_sql_str(f) for f in files)
        con.execute(f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM read_parquet([{file_list}])")


def sql_codes(values: list[str]) -> str:
    return ", ".join(_sql_str(v) for v in values)


def months_past_due() -> str:
    """SQL integer: months past due, NULL for non-numeric statuses (RA, unknown).

    A CASE is required: SQL does not guarantee left-to-right evaluation of AND, so
    `is_numeric AND CAST(...)` can still try to cast 'RA' and fail.
    """
    return (
        "(CASE WHEN regexp_full_match(delinquency_status, '[0-9]+') "
        "THEN CAST(delinquency_status AS INTEGER) END)"
    )


def default_status_condition(cfg: dict) -> str:
    """SQL boolean: does this loan-month's delinquency status mean default?"""
    return f"""(
        coalesce({months_past_due()} >= {int(cfg["default_dpd_months"])}, false)
        OR coalesce(delinquency_status IN ({sql_codes(cfg["default_status_codes"])}), false)
    )"""


def loans_query(cfg: dict) -> str:
    h = int(cfg["horizon_months"])
    cutoff = date.fromisoformat(cfg["performance_cutoff"])
    covid_start = date.fromisoformat(cfg["covid_window"]["start"])
    covid_end = date.fromisoformat(cfg["covid_window"]["end"])
    loss = sql_codes(cfg["loss_zero_balance_codes"])
    prepay = sql_codes(cfg["prepay_zero_balance_codes"])
    transfer = sql_codes(cfg["censor_zero_balance_codes"])
    max_gap = int(cfg["max_internal_gap_months"])
    if max_gap < 0:
        raise ValueError("max_internal_gap_months must be nonnegative")
    return f"""
WITH loan_window AS (
    SELECT
        loan_id,
        first_payment_date,
        CAST(first_payment_date + INTERVAL {h - 1} MONTH AS DATE) AS window_end
    FROM orig
),
history AS (
    -- Whole history of each loan. The exit is dated with zero_balance_date (the
    -- economic date), not with the reporting month of the row that carries the code.
    SELECT
        loan_id,
        max(period) AS last_period,
        arg_max(zero_balance_code, period) FILTER (WHERE zero_balance_code IS NOT NULL)
            AS exit_code,
        arg_max(coalesce(zero_balance_date, period), period)
            FILTER (WHERE zero_balance_code IS NOT NULL) AS exit_date
    FROM perf
    WHERE period <= DATE '{cutoff}'
    GROUP BY loan_id
),
window_status AS (
    -- Monthly statuses inside the observation window
    SELECT
        p.loan_id,
        min(p.period) FILTER (WHERE {default_status_condition(cfg)}) AS default_status_period,
        count(DISTINCT p.period) AS window_months_observed,
        bool_or(p.disaster_delinquency = 'Y') AS disaster_dlq_in_window,
        bool_or(p.assistance_plan = 'F') AS forbearance_in_window
    FROM perf p
    JOIN loan_window w USING (loan_id)
    WHERE p.period >= w.first_payment_date AND p.period <= w.window_end
      AND p.period <= DATE '{cutoff}'
    GROUP BY p.loan_id
),
gap_rows AS (
    SELECT
        loan_id,
        period,
        lag(period) OVER (PARTITION BY loan_id ORDER BY period) AS prev_period
    FROM perf
    WHERE period <= DATE '{cutoff}'
),
gaps AS (
    -- start gap: months between the first payment and the first record (seasoned
    --   acquisitions; leading gaps are tolerated under the D006 acquisition assumption)
    -- internal gap: longest run of missing months between two records, when the run
    --   starts inside the window (could hide a default that cured, D006)
    SELECT
        g.loan_id,
        greatest(
            0, date_diff('month', w.first_payment_date, min(g.period))
        ) AS start_gap_months,
        coalesce(
            max(date_diff('month', g.prev_period, g.period) - 1) FILTER (
                WHERE g.prev_period >= w.first_payment_date
                  AND g.prev_period < w.window_end
            ),
            0
        ) AS max_internal_gap_months
    FROM gap_rows g
    JOIN loan_window w USING (loan_id)
    WHERE g.period >= w.first_payment_date
    GROUP BY g.loan_id, w.first_payment_date
),
combined AS (
    SELECT
        o.* EXCLUDE (vantage_score),
        o.postal_code AS zip3,
        w.window_end,
        h.last_period,
        h.exit_code,
        h.exit_date,
        coalesce(h.exit_date BETWEEN w.first_payment_date AND w.window_end, false)
            AS exit_in_window,
        s.default_status_period,
        coalesce(s.window_months_observed, 0) AS window_months_observed,
        coalesce(s.disaster_dlq_in_window, false) AS disaster_dlq_in_window,
        coalesce(s.forbearance_in_window, false) AS forbearance_in_window,
        gp.start_gap_months,
        gp.max_internal_gap_months
    FROM orig o
    JOIN loan_window w USING (loan_id)
    LEFT JOIN history h USING (loan_id)
    LEFT JOIN window_status s USING (loan_id)
    LEFT JOIN gaps gp USING (loan_id)
),
labelled AS (
    SELECT
        *,
        CASE
            -- Checked FIRST: every labelled loan must have a complete window. Labelling
            -- early defaults while censoring loans that are still performing would
            -- over-represent defaults in recent vintages (selection bias, D004).
            WHEN window_end > DATE '{cutoff}' THEN 'censored_cutoff'
            WHEN (default_status_period IS NOT NULL
                  AND (NOT exit_in_window OR exit_code IN ({loss})
                       OR default_status_period <= exit_date))
              OR (exit_in_window AND exit_code IN ({loss})) THEN 'default'
            WHEN exit_in_window AND exit_code IN ({prepay}) THEN 'prepaid'
            WHEN exit_in_window AND exit_code IN ({transfer}) THEN 'censored_transfer'
            -- Observed at or after the window end with no default: performing, even if
            -- some months inside the window are missing (see window_months_observed).
            WHEN last_period >= window_end THEN 'performing'
            ELSE 'censored_gap'
        END AS outcome,
        least(
            default_status_period,
            CASE WHEN exit_in_window AND exit_code IN ({loss}) THEN exit_date END
        ) AS default_date
    FROM combined
)
SELECT
    *,
    CASE outcome WHEN 'default' THEN 1 WHEN 'prepaid' THEN 0 WHEN 'performing' THEN 0
    END::TINYINT AS default_24m,
    {h} - window_months_observed AS window_months_missing,
    (coalesce(harp = 'Y', false) OR pre_harp_loan_id IS NOT NULL) AS is_harp,
    year(first_payment_date) - vintage NOT IN (0, 1) AS date_anomaly,
    (first_payment_date <= DATE '{covid_end}' AND window_end >= DATE '{covid_start}')
        AS overlaps_covid,
    coalesce(max_internal_gap_months > {max_gap}, false) AS long_internal_gap
FROM labelled
"""


def build_loans(con: duckdb.DuckDBPyConnection, cfg: dict) -> None:
    """Create the in-memory table `loans` (with the in_pd_sample flag)."""
    con.execute(f"CREATE OR REPLACE TABLE loans AS {loans_query(cfg)}")
    con.execute("""
        ALTER TABLE loans ADD COLUMN in_pd_sample BOOLEAN;
        UPDATE loans SET in_pd_sample = default_24m IS NOT NULL
            AND NOT is_harp AND NOT date_anomaly AND NOT overlaps_covid
            -- A gap can hide a default; retain observed defaults (D006).
            AND NOT (long_internal_gap AND default_24m = 0) AND zip3 IS NOT NULL;
    """)


def summary_markdown(con: duckdb.DuckDBPyConnection) -> str:
    def table(query: str) -> str:
        cur = con.execute(query)
        headers = [d[0] for d in cur.description]
        rows = cur.fetchall()
        lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
        lines += ["| " + " | ".join(str(v) for v in r) + " |" for r in rows]
        return "\n".join(lines)

    return (
        "\n\n".join(
            [
                "# Target summary",
                "Generated by `python -m climrisk.features.target`. Aggregate statistics only.",
                "## Outcomes",
                table("""SELECT outcome, count(*) AS loans,
                        round(100.0 * count(*) / sum(count(*)) OVER (), 2) AS pct
                 FROM loans GROUP BY 1 ORDER BY loans DESC"""),
                "## Default rate by vintage (labelled loans)",
                table("""SELECT vintage, count(default_24m) AS labelled,
                        sum(default_24m) AS defaults,
                        round(100.0 * avg(default_24m), 2) AS default_rate_pct,
                        count(*) FILTER (WHERE in_pd_sample) AS in_pd_sample
                 FROM loans GROUP BY 1 ORDER BY 1"""),
                "## Missing months inside the window (performing loans)",
                table("""SELECT CASE WHEN window_months_missing = 0 THEN '0'
                             WHEN window_months_missing <= 2 THEN '1-2'
                             WHEN window_months_missing <= 6 THEN '3-6'
                             ELSE '7+' END AS missing_months,
                        count(*) AS loans
                 FROM loans WHERE outcome = 'performing' GROUP BY 1 ORDER BY 1"""),
                "## Gaps in the monthly history (labelled loans)",
                table("""SELECT CASE WHEN start_gap_months = 0 THEN '0'
                             WHEN start_gap_months <= 6 THEN '1-6'
                             ELSE '7+' END AS start_gap,
                        CASE WHEN max_internal_gap_months = 0 THEN '0'
                             WHEN max_internal_gap_months <= 2 THEN '1-2'
                             ELSE '3+' END AS max_internal_gap,
                        count(*) AS loans,
                        sum(default_24m) AS defaults
                 FROM loans WHERE default_24m IS NOT NULL GROUP BY ALL ORDER BY ALL"""),
                "## PD sample exclusions (labelled loans)",
                table("""SELECT count(*) FILTER (WHERE is_harp) AS harp,
                        count(*) FILTER (WHERE date_anomaly) AS date_anomaly,
                        count(*) FILTER (WHERE overlaps_covid) AS overlaps_covid,
                        count(*) FILTER (WHERE long_internal_gap) AS long_internal_gap,
                        count(*) FILTER (WHERE zip3 IS NULL) AS unknown_zip3,
                        count(*) FILTER (WHERE in_pd_sample) AS in_pd_sample
                 FROM loans WHERE default_24m IS NOT NULL"""),
            ]
        )
        + "\n"
    )


def run(
    data_dir: Path = INTERIM_DIR,
    out_dir: Path = PROCESSED_DIR,
    report_path: Path = REPORT_PATH,
    cfg: dict | None = None,
) -> Path:
    cfg = cfg or load_config()
    with duckdb.connect() as con:
        register_views(con, data_dir)
        build_loans(con, cfg)

        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / "loans.parquet"
        tmp = target.with_name(target.name + ".tmp")
        try:
            con.execute(f"COPY loans TO {_sql_str(tmp)} (FORMAT PARQUET, COMPRESSION ZSTD)")
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)

        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(summary_markdown(con), encoding="utf-8")
        n = con.execute("SELECT count(*) FROM loans").fetchone()[0]
        log.info("Wrote %s loans to %s; summary in %s", f"{n:,}", target, report_path)
    return target


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()


if __name__ == "__main__":
    main()
