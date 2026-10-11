"""Download and parse OpenFEMA Disaster Declarations Summaries v2 (D009).

Preserve the UTC calendar date from ISO strings. Household assistance uses IA or IH.
County code 000 identifies statewide declarations.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import duckdb
import yaml

from climrisk.ingest.download import download
from climrisk.ingest.freddie import ROOT, _sql_str

CONFIG_PATH = ROOT / "configs" / "climate.yaml"
RAW_PATH = ROOT / "data" / "raw" / "fema" / "DisasterDeclarationsSummaries.csv"
OUT_PATH = ROOT / "data" / "interim" / "fema" / "declarations.parquet"
log = logging.getLogger(__name__)


def iso_date(column: str) -> str:
    """Read an ISO string's date without local timezone conversion."""
    return f"CAST(NULLIF(substr({column}, 1, 10), '') AS DATE)"


def parse(src: Path = RAW_PATH, dst: Path = OUT_PATH) -> int:
    query = f"""
        SELECT CAST(disasterNumber AS INTEGER) AS disaster_number,
            declarationType AS declaration_type, incidentType AS incident_type, state,
            lpad(fipsStateCode, 2, '0') || lpad(fipsCountyCode, 3, '0') AS county_fips,
            lpad(fipsCountyCode, 3, '0') = '000' AS statewide,
            {iso_date("declarationDate")} AS declaration_date,
            {iso_date("incidentBeginDate")} AS incident_begin,
            {iso_date("incidentEndDate")} AS incident_end,
            (coalesce(lower(iaProgramDeclared) IN ('1', 'true'), false)
             OR coalesce(lower(ihProgramDeclared) IN ('1', 'true'), false))
                AS household_assistance,
            coalesce(lower(paProgramDeclared) IN ('1', 'true'), false) AS public_assistance,
            declarationTitle AS title, designatedArea AS designated_area
        FROM read_csv({_sql_str(src)}, header=true, all_varchar=true)
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
    log.info("Parsed %s declaration rows -> %s", f"{n:,}", dst)
    return n


def main() -> None:
    parser = argparse.ArgumentParser(description="Download and parse FEMA declarations")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        url = yaml.safe_load(f)["sources"]["fema_declarations_url"]
    download(url, RAW_PATH, refresh=args.refresh)
    parse()


if __name__ == "__main__":
    main()
