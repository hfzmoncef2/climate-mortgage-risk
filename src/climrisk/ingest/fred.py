"""Freddie Mac PMMS 30-year mortgage rate via FRED: monthly averages (Phase 6).

Output: data/interim/fred/pmms_monthly.parquet (month, pmms_rate in percent).
Usage: uv run --frozen python -m climrisk.ingest.fred [--refresh]
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import duckdb
import yaml

from climrisk.ingest.download import download
from climrisk.ingest.freddie import ROOT, _sql_str

CONFIG_PATH = ROOT / "configs" / "pricing.yaml"
RAW_PATH = ROOT / "data" / "raw" / "fred" / "MORTGAGE30US.csv"
OUT_PATH = ROOT / "data" / "interim" / "fred" / "pmms_monthly.parquet"

log = logging.getLogger(__name__)


def parse(src: Path = RAW_PATH, dst: Path = OUT_PATH) -> int:
    """Average weekly observations by month; FRED marks missing weeks with '.'."""
    query = f"""
        SELECT CAST(date_trunc('month', CAST(observation_date AS DATE)) AS DATE) AS month,
               avg(CAST(NULLIF(MORTGAGE30US, '.') AS DOUBLE)) AS pmms_rate
        FROM read_csv({_sql_str(src)}, header = true, all_varchar = true)
        GROUP BY 1 HAVING count(NULLIF(MORTGAGE30US, '.')) > 0
        ORDER BY 1
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    try:
        with duckdb.connect() as con:
            con.execute(f"COPY ({query}) TO {_sql_str(tmp)} (FORMAT PARQUET)")
            n = con.execute(f"SELECT count(*) FROM read_parquet({_sql_str(tmp)})").fetchone()[0]
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)
    log.info("Wrote %s monthly PMMS rates -> %s", n, dst)
    return n


def main() -> None:
    parser = argparse.ArgumentParser(description="Download and average the PMMS rate")
    parser.add_argument("--refresh", action="store_true", help="re-download the raw file")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        url = yaml.safe_load(f)["sources"]["pmms_url"]
    download(url, RAW_PATH, refresh=args.refresh)
    parse()


if __name__ == "__main__":
    main()
