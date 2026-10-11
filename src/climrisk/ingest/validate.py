"""Validate ingested Freddie Mac Parquet files.

Blocking checks cause exit code 1; warnings document anomalies without blocking.
Write aggregate statistics only to reports/data_quality.md.

Usage: uv run --frozen python -m climrisk.ingest.validate
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb

from climrisk.ingest.freddie import OUT_DIR, ROOT, load_schema

REPORT_PATH = ROOT / "reports" / "data_quality.md"
PERFORMANCE_CUTOFF = date(2026, 3, 1)  # D001: performance disclosed through 2026-03-31

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Check:
    name: str
    query: str  # Return one number: the count of violations.
    blocking: bool


CHECKS = [
    # Blocking checks: key integrity.
    Check(
        "Loan ID never NULL (origination)",
        "SELECT count(*) FROM orig WHERE loan_id IS NULL",
        blocking=True,
    ),
    Check(
        "Unique loan ID across all origination vintages",
        "SELECT count(*) FROM (SELECT loan_id FROM orig GROUP BY loan_id HAVING count(*) > 1)",
        blocking=True,
    ),
    Check(
        "Identifier vintage equals source file year (D003)",
        "SELECT count(*) FROM orig WHERE vintage <> source_year",
        blocking=False,
    ),
    Check(
        "First payment year is zero or one year after vintage",
        "SELECT count(*) FROM orig WHERE year(first_payment_date) - vintage NOT IN (0, 1)",
        blocking=False,
    ),
    Check(
        "Unique loan-month pair (performance)",
        "SELECT count(*) FROM (SELECT 1 FROM perf GROUP BY loan_id, period HAVING count(*) > 1)",
        blocking=True,
    ),
    Check(
        "Every performance loan exists in origination",
        "SELECT count(DISTINCT loan_id) FROM perf ANTI JOIN orig USING (loan_id)",
        blocking=True,
    ),
    Check(
        "No performance month after the cutoff",
        f"SELECT count(*) FROM perf WHERE period > DATE '{PERFORMANCE_CUTOFF}'",
        blocking=True,
    ),
    # Warnings: values outside expected ranges.
    Check(
        "FICO within [300, 850]",
        "SELECT count(*) FROM orig WHERE fico NOT BETWEEN 300 AND 850",
        blocking=False,
    ),
    Check(
        "VantageScore within [300, 850]",
        "SELECT count(*) FROM orig WHERE vantage_score NOT BETWEEN 300 AND 850",
        blocking=False,
    ),
    Check(
        "LTV within [1, 998]",
        "SELECT count(*) FROM orig WHERE ltv NOT BETWEEN 1 AND 998",
        blocking=False,
    ),
    Check(
        "CLTV >= LTV",
        "SELECT count(*) FROM orig WHERE cltv < ltv",
        blocking=False,
    ),
    Check(
        "DTI within [0, 65]",
        "SELECT count(*) FROM orig WHERE dti NOT BETWEEN 0 AND 65",
        blocking=False,
    ),
    Check(
        "Origination interest rate within (0, 20]",
        "SELECT count(*) FROM orig WHERE orig_rate <= 0 OR orig_rate > 20",
        blocking=False,
    ),
    Check(
        "Current UPB never negative",
        "SELECT count(*) FROM perf WHERE current_upb < 0",
        blocking=False,
    ),
    Check(
        "Loans with no performance history",
        "SELECT count(*) FROM orig ANTI JOIN (SELECT DISTINCT loan_id FROM perf) USING (loan_id)",
        blocking=False,
    ),
]

# Profiles to review before phase 2 modeling decisions.
PROFILES = {
    "Loans and monthly rows by vintage": """
        SELECT o.vintage AS vintage, o.loans, p.monthly_rows
        FROM (SELECT vintage, count(*) AS loans FROM orig GROUP BY vintage) o
        JOIN (SELECT vintage, count(*) AS monthly_rows FROM perf GROUP BY vintage) p
          USING (vintage)
        ORDER BY vintage""",
    "Delinquency status: 15 most frequent values": """
        SELECT coalesce(delinquency_status, 'NULL') AS status, count(*) AS rows
        FROM perf GROUP BY 1 ORDER BY rows DESC LIMIT 15""",
    "Zero balance codes": """
        SELECT coalesce(zero_balance_code, 'NULL') AS code, count(*) AS rows
        FROM perf GROUP BY 1 ORDER BY rows DESC""",
    "Borrower assistance plans": """
        SELECT coalesce(assistance_plan, 'NULL') AS plan, count(*) AS rows
        FROM perf GROUP BY 1 ORDER BY rows DESC""",
    "Disaster-related delinquency (disaster_delinquency = 'Y') by year": """
        SELECT year(period) AS year, count(*) AS rows
        FROM perf WHERE disaster_delinquency = 'Y' GROUP BY 1 ORDER BY 1""",
    "Unknown ZIP3 by vintage": """
        SELECT vintage AS vintage, count(*) FILTER (WHERE postal_code IS NULL) AS unknown_zip3
        FROM orig GROUP BY 1 ORDER BY 1""",
}


PROFILES["Unexpected first-payment year offsets from vintage"] = """
    SELECT year(first_payment_date) - vintage AS payment_year_offset, count(*) AS loans
    FROM orig
    WHERE year(first_payment_date) - vintage NOT IN (0, 1)
    GROUP BY 1 ORDER BY 1
"""


def register_views(con: duckdb.DuckDBPyConnection, data_dir: Path) -> None:
    """Register all Parquet files as orig and perf SQL views."""
    for view, table in (("orig", "origination"), ("perf", "performance")):
        files = sorted((data_dir / table).glob("*.parquet"))
        if not files:
            raise FileNotFoundError(f"No Parquet files in {data_dir / table} : run `make data`")
        file_list = ", ".join("'" + str(f).replace("'", "''") + "'" for f in files)
        con.execute(f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM read_parquet([{file_list}])")


def missing_rates(con: duckdb.DuckDBPyConnection, view: str, columns: list[str]) -> list[tuple]:
    """Return missing percentages by column, highest first."""
    exprs = ", ".join(f'100.0 * avg(("{c}" IS NULL)::INT) AS "{c}"' for c in columns)
    row = con.execute(f"SELECT {exprs} FROM {view}").fetchone()
    return sorted(zip(columns, row, strict=True), key=lambda x: -x[1])


def to_markdown(headers: list[str], rows: list[tuple]) -> str:
    def fmt(v: object) -> str:
        if isinstance(v, float):
            return f"{v:.2f}"
        if (
            isinstance(v, int) and abs(v) >= 10_000
        ):  # Group thousands without formatting calendar years.
            return f"{v:,}".replace(",", " ")
        return str(v)

    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(fmt(v) for v in row) + " |" for row in rows]
    return "\n".join(lines)


def run(data_dir: Path = OUT_DIR, report_path: Path = REPORT_PATH) -> bool:
    """Run checks and write the report; return False on blocking failures."""
    con = duckdb.connect()
    register_views(con, data_dir)
    schema = load_schema()

    results = []
    for check in CHECKS:
        violations = con.execute(check.query).fetchone()[0]
        level = "BLOCKING" if check.blocking else "warning"
        status = "OK" if violations == 0 else f"FAIL ({level})"
        results.append((check.name, violations, status))
        log.info("%-60s %s", check.name, status if violations == 0 else f"{status} : {violations}")

    sections = [
        "# Freddie Mac data quality report",
        "Generated by `python -m climrisk.ingest.validate`. Aggregate statistics only.",
        "## Checks",
        to_markdown(["Check", "Violations", "Status"], results),
    ]
    for title, query in PROFILES.items():
        cursor = con.execute(query)
        headers = [d[0] for d in cursor.description]
        sections += [f"## {title}", to_markdown(headers, cursor.fetchall())]
    for view, table in (("orig", "origination"), ("perf", "performance")):
        columns = [c["name"] for c in schema[table]]
        sections += [
            f"## Missing values ({table}, %)",
            to_markdown(["Column", "% missing"], missing_rates(con, view, columns)),
        ]

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n\n".join(sections) + "\n", encoding="utf-8")
    log.info("Report written: %s", report_path)

    blocking_failures = [
        name for (name, n, _), c in zip(results, CHECKS, strict=True) if n and c.blocking
    ]
    return not blocking_failures


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(0 if run() else 1)


if __name__ == "__main__":
    main()
