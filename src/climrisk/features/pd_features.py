"""Feature table for the PD model (Phase 7).

One row per loan of the PD sample (D004-D006), with only information known at origination:
    borrower / loan   FICO, LTV, CLTV, DTI, log amount, term, MI %, borrowers, units,
                      spread over PMMS (D017), purpose, occupancy, channel, property type,
                      first-time buyer, super-conforming, state
    climate           exposure_main / exposure_broad (10 years, D009, D011), disasters in the
                      last 12 months (same availability lag), hurricane-coast share and
                      log distance (D016)
    split             train / validation / test by vintage (configs/pd_model.yaml)

Excluded on purpose (not known at origination, or unstable over time): anything from the
performance file, the window flags, start_gap, seller name, valuation method, vintage.

Output: data/processed/pd_features.parquet

Usage:
    uv run --frozen python -m climrisk.features.pd_features
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb
import yaml

from climrisk.features import climate
from climrisk.features.target import PROCESSED_DIR
from climrisk.ingest.freddie import ROOT

CONFIG_PATH = ROOT / "configs" / "pd_model.yaml"
PMMS_PATH = ROOT / "data" / "interim" / "fred" / "pmms_monthly.parquet"
PRICING_CONFIG = ROOT / "configs" / "pricing.yaml"

log = logging.getLogger(__name__)


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def feature_columns(cfg: dict) -> tuple[list[str], list[str]]:
    """(base features, climate features)."""
    f = cfg["features"]
    return f["numeric"] + f["categorical"], f["climate"]


def split_case(cfg: dict) -> str:
    whens = " ".join(
        f"WHEN l.vintage BETWEEN {lo} AND {hi} THEN '{name}'"
        for name, (lo, hi) in cfg["split"].items()
    )
    return f"CASE {whens} ELSE 'unused' END"


def features_query(
    cfg: dict, climate_cfg: dict, origination_lag: int, processed_dir: Path, pmms_path: Path
) -> str:
    e = climate_cfg["exposure"]
    lag = int(e.get("availability_lag_months", 0))
    recent = int(cfg["features"]["recent_months"])

    def cat(col: str) -> str:
        return f"coalesce(CAST(l.{col} AS VARCHAR), 'missing') AS {col}"

    categorical = ",\n    ".join(cat(c) for c in cfg["features"]["categorical"])
    return f"""
WITH loans AS (
    SELECT *, CAST(first_payment_date - INTERVAL {origination_lag} MONTH AS DATE) AS orig_month
    FROM read_parquet('{processed_dir / "loans.parquet"}')
    WHERE in_pd_sample
),
clim AS (
    SELECT zip3, month, exposure_main, exposure_broad,
           coalesce(sum(events_main) OVER (PARTITION BY zip3 ORDER BY month
               ROWS BETWEEN {recent + lag} PRECEDING AND {lag + 1} PRECEDING), 0)
               AS recent_events_12m
    FROM read_parquet('{processed_dir / "zip3_month_climate.parquet"}')
)
SELECT
    l.loan_id,
    l.vintage,
    {split_case(cfg)} AS split,
    l.default_24m,
    l.fico, l.ltv, l.cltv, l.dti,
    ln(l.orig_upb) AS log_upb,
    l.orig_term, l.mi_pct, l.num_borrowers, l.num_units,
    100.0 * (l.orig_rate - m.pmms_rate) AS spread_bp,
    {categorical},
    c.exposure_main, c.exposure_broad, c.recent_events_12m,
    k.near_coast_share,
    ln(1 + k.dist_coast_km) AS log_dist_coast
FROM loans l
LEFT JOIN read_parquet('{pmms_path}') m ON m.month = l.orig_month
LEFT JOIN clim c ON c.zip3 = l.zip3 AND c.month = l.orig_month
LEFT JOIN read_parquet('{processed_dir / "zip3_coast.parquet"}') k ON k.zip3 = l.zip3
ORDER BY l.loan_id
"""


def summary(con: duckdb.DuckDBPyConnection, path: Path, cfg: dict) -> str:
    rows = con.execute(f"""
        SELECT split, count(*) AS loans, sum(default_24m) AS defaults,
               round(100 * avg(default_24m), 2) AS default_pct
        FROM read_parquet('{path}') GROUP BY split ORDER BY min(vintage)
    """).fetchall()
    lines = ["split      loans  defaults  default_pct"]
    lines += [f"{s:<10} {n:>7,} {d:>8,} {p:>8.2f}" for s, n, d, p in rows]
    numeric = cfg["features"]["numeric"] + cfg["features"]["climate"]
    nulls = con.execute(
        "SELECT "
        + ", ".join(f"count(*) FILTER (WHERE {c} IS NULL)" for c in numeric)
        + f" FROM read_parquet('{path}')"
    ).fetchone()
    lines.append(
        "missing values: "
        + ", ".join(f"{c}={n:,}" for c, n in zip(numeric, nulls, strict=True) if n)
    )
    return "\n".join(lines)


def run(
    processed_dir: Path = PROCESSED_DIR,
    pmms_path: Path = PMMS_PATH,
    cfg: dict | None = None,
    climate_cfg: dict | None = None,
    origination_lag: int | None = None,
) -> Path:
    cfg = cfg or load_config()
    climate_cfg = climate_cfg or climate.load_config()
    if origination_lag is None:
        with open(PRICING_CONFIG, encoding="utf-8") as f:
            origination_lag = int(yaml.safe_load(f)["pricing"]["origination_lag_months"])
    target = processed_dir / "pd_features.parquet"
    tmp = target.with_name(target.name + ".tmp")
    con = duckdb.connect()
    query = features_query(cfg, climate_cfg, origination_lag, processed_dir, pmms_path)
    con.execute(f"COPY ({query}) TO '{tmp}' (FORMAT PARQUET)")
    tmp.replace(target)
    log.info("Wrote %s\n%s", target, summary(con, target, cfg))
    return target


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()


if __name__ == "__main__":
    main()
