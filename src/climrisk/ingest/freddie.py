"""Ingest Freddie Mac SFLLD sample files into typed Parquet.

Read sample_orig_YYYY.txt and sample_perf_YYYY.txt for each source year,
apply configs/freddie_schema.yaml (names, types, unknown codes -> NULL), and write:
    data/interim/freddie/origination/YYYY.parquet
    data/interim/freddie/performance/YYYY.parquet

Usage:
    uv run --frozen python -m climrisk.ingest.freddie --years 2005
    uv run --frozen python -m climrisk.ingest.freddie --all
"""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

import duckdb
import yaml

ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = ROOT / "configs" / "freddie_schema.yaml"
RAW_DIR = ROOT / "data" / "raw" / "freddie"
OUT_DIR = ROOT / "data" / "interim" / "freddie"

TABLES = {
    "origination": "sample_orig_{year}.txt",
    "performance": "sample_perf_{year}.txt",
}

# Schema types -> DuckDB types. Use compact integers to reduce memory and storage.
DUCKDB_TYPES = {
    "string": "VARCHAR",
    "Int8": "TINYINT",
    "Int16": "SMALLINT",
    "Int32": "INTEGER",
    "Float64": "DOUBLE",
}

log = logging.getLogger(__name__)


def load_schema(path: Path = SCHEMA_PATH) -> dict:
    """Load the YAML schema and require consecutive column positions from 1 to N."""
    with open(path, encoding="utf-8") as f:
        schema = yaml.safe_load(f)
    for table in TABLES:
        positions = [col["position"] for col in schema[table]]
        if positions != list(range(1, len(positions) + 1)):
            raise ValueError(f"{table} : missing or unordered positions")
        unknown = {col["dtype"] for col in schema[table]} - set(DUCKDB_TYPES) - {"yyyymm"}
        if unknown:
            raise ValueError(f"{table} : unknown types {unknown}")
    return schema


def _sql_str(value: object) -> str:
    """Escape a Python value as a single-quoted SQL literal."""
    return "'" + str(value).replace("'", "''") + "'"


def column_expression(col: dict) -> str:
    """Build a SQL expression to clean and cast a raw text column.

    Trim whitespace, replace empty and unknown codes with NULL, and cast strictly.
    YYYYMM becomes the first day of the month. Unexpected values raise errors.
    """
    name = col["name"]
    raw = f'TRIM("{name}")'
    null_if = [f"{raw} = ''"]
    if col["missing_codes"]:
        codes = ", ".join(_sql_str(code) for code in col["missing_codes"])
        null_if.append(f"{raw} IN ({codes})")
    value = f"CASE WHEN {' OR '.join(null_if)} THEN NULL ELSE {raw} END"

    if col["dtype"] == "yyyymm":
        value = f"CAST(strptime({value}, '%Y%m') AS DATE)"
    elif col["dtype"] != "string":
        value = f"CAST({value} AS {DUCKDB_TYPES[col['dtype']]})"
    return f'{value} AS "{name}"'


def build_query(columns: list[dict], source: Path, year: int) -> str:
    """Read a raw file as text and return typed columns.

    D003: derive vintage and origination quarter from PYYQnXXXXXXX.
    Preserve the source file year as source_year for traceability.
    The century pivot supports 1990-2089, including the current 2004-2024 samples.
    """
    names = ", ".join(f"{_sql_str(col['name'])}: 'VARCHAR'" for col in columns)
    select = ",\n    ".join(column_expression(col) for col in columns)
    yy = """CAST(substr(TRIM("loan_id"), 2, 2) AS SMALLINT)"""
    return f"""
SELECT
    {select},
    CAST({yy} + CASE WHEN {yy} >= 90 THEN 1900 ELSE 2000 END AS SMALLINT) AS vintage,
    CAST(substr(TRIM("loan_id"), 5, 1) AS TINYINT) AS orig_quarter,
    CAST({year} AS SMALLINT) AS source_year
FROM read_csv(
    {_sql_str(source)},
    delim = '|',
    header = false,
    columns = {{{names}}},
    auto_detect = false
)"""


def ingest_year(
    con: duckdb.DuckDBPyConnection,
    schema: dict,
    year: int,
    raw_dir: Path = RAW_DIR,
    out_dir: Path = OUT_DIR,
) -> dict[str, int]:
    """Convert both tables for a source year to Parquet and return their row counts."""
    counts = {}
    for table, pattern in TABLES.items():
        source = raw_dir / pattern.format(year=year)
        if not source.exists():
            raise FileNotFoundError(source)

        target = out_dir / table / f"{year}.parquet"
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temporary file, then atomically replace this table’s output.
        tmp = target.with_name(target.name + ".tmp")

        start = time.perf_counter()
        query = build_query(schema[table], source, year)
        try:
            con.execute(f"COPY ({query}) TO {_sql_str(tmp)} (FORMAT PARQUET, COMPRESSION ZSTD)")
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)

        counts[table] = con.execute(
            f"SELECT count(*) FROM read_parquet({_sql_str(target)})"
        ).fetchone()[0]
        elapsed = time.perf_counter() - start
        log.info("%s %d : %s rows (%.1f s)", table, year, f"{counts[table]:,}", elapsed)
    return counts


def available_years(raw_dir: Path = RAW_DIR) -> list[int]:
    """Return source years with an available origination file."""
    return sorted(int(p.stem.split("_")[-1]) for p in raw_dir.glob("sample_orig_*.txt"))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Ingest Freddie Mac samples into Parquet")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--years", type=int, nargs="+", help="e.g. --years 2005 2006")
    group.add_argument("--all", action="store_true", help="all source years in data/raw")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    schema = load_schema()
    years = available_years() if args.all else args.years
    if not years:
        raise SystemExit(f"No sample_orig_*.txt files in {RAW_DIR}")

    with duckdb.connect() as con:
        for year in years:
            ingest_year(con, schema, year)
    log.info("Completed: %d source year(s)", len(years))


if __name__ == "__main__":
    main()
