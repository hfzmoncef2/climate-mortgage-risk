"""Download and parse Census 2010 ZCTA-county housing counts (D002); no API key."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import duckdb
import yaml

from climrisk.ingest.download import download
from climrisk.ingest.freddie import ROOT, _sql_str

CONFIG_PATH = ROOT / "configs" / "climate.yaml"
RAW_PATH = ROOT / "data" / "raw" / "geo" / "zcta_county_rel_10.txt"
OUT_PATH = ROOT / "data" / "interim" / "geo" / "zcta_county.parquet"
log = logging.getLogger(__name__)


def parse(src: Path = RAW_PATH, dst: Path = OUT_PATH) -> int:
    """Read actual housing units (HUPT) for each ZCTA-county part."""
    query = f"""
        SELECT ZCTA5 AS zcta, GEOID AS county_fips, CAST(HUPT AS BIGINT) AS housing_units
        FROM read_csv({_sql_str(src)}, delim=',', header=true, all_varchar=true)
        WHERE ZCTA5 IS NOT NULL AND ZCTA5 <> ''
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    with duckdb.connect() as con:
        try:
            con.execute(f"COPY ({query}) TO {_sql_str(tmp)} (FORMAT PARQUET, COMPRESSION ZSTD)")
            tmp.replace(dst)
        finally:
            tmp.unlink(missing_ok=True)
        n = con.execute("SELECT count(*) FROM read_parquet(?)", [str(dst)]).fetchone()[0]
    log.info("Wrote %s ZCTA-county parts -> %s", f"{n:,}", dst)
    return n


def main() -> None:
    parser = argparse.ArgumentParser(description="Download and parse Census 2010 ZCTA-county data")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        url = yaml.safe_load(f)["sources"]["zcta_county_url"]
    download(url, RAW_PATH, refresh=args.refresh)
    parse()


if __name__ == "__main__":
    main()
