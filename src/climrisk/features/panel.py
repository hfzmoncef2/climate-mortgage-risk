"""Build the additive ZIP3-month panel for subsequent causal analysis.

Keep months on or after first payment, omit unknown ZIP3, and retain COVID months.
Compute rates at analysis time from the aggregated counts.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import duckdb

from climrisk.features.target import (
    INTERIM_DIR,
    PROCESSED_DIR,
    load_config,
    months_past_due,
    register_views,
    sql_codes,
)
from climrisk.ingest.freddie import _sql_str

log = logging.getLogger(__name__)


def panel_query(cfg: dict) -> str:
    dpd = months_past_due()
    reo = f"delinquency_status IN ({sql_codes(cfg['default_status_codes'])})"
    cutoff = date.fromisoformat(cfg["performance_cutoff"])
    return f"""
WITH rows AS (
    SELECT p.*, o.postal_code AS zip3, {dpd} AS dpd, {reo} AS is_reo
    FROM perf p JOIN orig o USING (loan_id)
    WHERE o.postal_code IS NOT NULL AND p.period >= o.first_payment_date
      AND p.period <= DATE '{cutoff}'
)
SELECT zip3, period,
    count(*) AS n_loans,
    count(*) FILTER (WHERE dpd >= 1 OR is_reo) AS n_dlq30,
    count(*) FILTER (WHERE dpd >= 2 OR is_reo) AS n_dlq60,
    count(*) FILTER (WHERE dpd >= 3 OR is_reo) AS n_dlq90,
    count(*) FILTER (
        WHERE zero_balance_code IN ({sql_codes(cfg["prepay_zero_balance_codes"])})
    ) AS n_prepaid,
    count(*) FILTER (
        WHERE zero_balance_code IN ({sql_codes(cfg["loss_zero_balance_codes"])})
    ) AS n_loss_exit,
    count(*) FILTER (WHERE disaster_delinquency = 'Y') AS n_disaster_dlq,
    count(*) FILTER (WHERE assistance_plan = 'F') AS n_forbearance,
    sum(current_upb) AS upb
FROM rows GROUP BY zip3, period ORDER BY zip3, period
"""


def run(
    data_dir: Path = INTERIM_DIR, out_dir: Path = PROCESSED_DIR, cfg: dict | None = None
) -> Path:
    cfg = cfg or load_config()
    with duckdb.connect() as con:
        register_views(con, data_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / "zip3_month.parquet"
        tmp = target.with_name(target.name + ".tmp")
        try:
            con.execute(
                f"COPY ({panel_query(cfg)}) TO {_sql_str(tmp)} (FORMAT PARQUET, COMPRESSION ZSTD)"
            )
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)
        n, z = con.execute(
            "SELECT count(*), count(DISTINCT zip3) FROM read_parquet(?)", [str(target)]
        ).fetchone()
        log.info("Wrote %s ZIP3-month rows (%d ZIP3) to %s", f"{n:,}", z, target)
    return target


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()


if __name__ == "__main__":
    main()
