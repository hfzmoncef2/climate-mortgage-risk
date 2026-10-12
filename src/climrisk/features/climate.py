"""Climate exposure and disaster calendar by ZIP3 and month (Phase 3).

1. ZIP3 -> county weights (D002): 2010 housing units of each ZCTA-county part,
   summed by ZIP3 and county, normalised to sum to 1 per ZIP3.
2. Disaster calendar (D009): qualifying FEMA declarations at county level, dated by the
   month the incident began, mapped to ZIP3 with the weights.
3. Ex-ante exposure (D010): disasters in the `lookback_months` BEFORE each month, so a
   month t excludes current and future incident months. Historical declaration
   revisions and static 2010 geographic weights remain retrospective inputs.

Outputs:
    data/processed/zip3_county_weights.parquet
    data/processed/zip3_month_climate.parquet
        zip3, month,
        hit_share_<def>   share of the ZIP3's housing in counties hit that month (0-1)
        events_<def>      expected number of disasters hitting a home that month
        exposure_<def>    lagged rolling sum of events_<def> (D011)
    reports/climate_summary.md
for <def> in {main, broad} (configs/climate.yaml).

Usage:
    uv run --frozen python -m climrisk.features.climate
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb
import yaml

from climrisk.features.target import PROCESSED_DIR, sql_codes
from climrisk.ingest.freddie import ROOT, _sql_str

CONFIG_PATH = ROOT / "configs" / "climate.yaml"
GEO_DIR = ROOT / "data" / "interim" / "geo"
FEMA_PATH = ROOT / "data" / "interim" / "fema" / "declarations.parquet"
REPORT_PATH = ROOT / "reports" / "climate_summary.md"
DEFINITIONS = ("main", "broad")

log = logging.getLogger(__name__)


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def weights_query(geo_dir: Path, cfg: dict) -> str:
    """Housing weights, supplemented by documented weights for ZIP3 without a ZCTA."""
    manual = cfg.get("zip3_manual_weights") or {}
    manual_rows = ", ".join(
        f"({_sql_str(zip3)}, {_sql_str(county)}, {float(weight)})"
        for zip3, counties in manual.items()
        for county, weight in counties.items()
    )
    manual_sql = (
        f"""
UNION ALL
SELECT m.zip3, m.county_fips, NULL AS housing, m.weight
FROM (VALUES {manual_rows}) AS m(zip3, county_fips, weight)
WHERE m.zip3 NOT IN (SELECT zip3 FROM by_county)"""
        if manual_rows
        else ""
    )
    return f"""
WITH by_county AS (
    SELECT substr(zcta, 1, 3) AS zip3, county_fips, sum(housing_units) AS housing
    FROM read_parquet({_sql_str(geo_dir / "zcta_county.parquet")})
    GROUP BY zip3, county_fips
    HAVING sum(housing_units) > 0
)
SELECT zip3, county_fips, housing, housing / sum(housing) OVER (PARTITION BY zip3) AS weight
FROM by_county{manual_sql}
"""


def county_expression(cfg: dict) -> str:
    """SQL: FEMA county code mapped to the 2010 code of the same area (configs county_recode)."""
    recode = cfg.get("county_recode") or {}
    if not recode:
        return "county_fips"
    cases = " ".join(f"WHEN '{new}' THEN '{old}'" for new, old in recode.items())
    return f"CASE county_fips {cases} ELSE county_fips END"


def events_query(cfg: dict, definition: str, fema_path: Path) -> str:
    """County-level qualifying disasters, one row per (disaster, county, start month)."""
    d = cfg["events"][definition]
    household = "AND household_assistance" if d["require_household_assistance"] else ""
    return f"""
SELECT DISTINCT
    disaster_number,
    {county_expression(cfg)} AS county_fips,
    CAST(date_trunc('month', incident_begin) AS DATE) AS month
FROM read_parquet({_sql_str(fema_path)})
WHERE declaration_type IN ({sql_codes(cfg["events"]["declaration_types"])})
  AND incident_type IN ({sql_codes(d["incident_types"])})
  AND NOT statewide
  {household}
"""


def climate_query(cfg: dict) -> str:
    e = cfg["exposure"]
    lookback = int(e["lookback_months"])
    lag = int(e.get("availability_lag_months", 0))
    hits = []
    for d in DEFINITIONS:
        hits.append(f"""
hits_{d} AS (
    SELECT
        w.zip3,
        ev.month,
        sum(w.weight) FILTER (WHERE ev.first_in_month) AS hit_share_{d},
        sum(w.weight)                                 AS events_{d}
    FROM (
        SELECT *, row_number() OVER (PARTITION BY county_fips, month) = 1 AS first_in_month
        FROM events_{d}
    ) ev
    JOIN weights w USING (county_fips)
    GROUP BY w.zip3, ev.month
)""")
    joins = "\n".join(f"LEFT JOIN hits_{d} USING (zip3, month)" for d in DEFINITIONS)
    cols = ",\n    ".join(
        f"coalesce(hit_share_{d}, 0) AS hit_share_{d}, coalesce(events_{d}, 0) AS events_{d}"
        for d in DEFINITIONS
    )
    exposure = ",\n    ".join(
        f"coalesce(sum(events_{d}) OVER (PARTITION BY zip3 ORDER BY month "
        f"ROWS BETWEEN {lookback + lag} PRECEDING AND {lag + 1} PRECEDING), 0) AS exposure_{d}"
        for d in DEFINITIONS
    )
    return f"""
WITH
grid AS (
    SELECT z.zip3, CAST(m.month AS DATE) AS month
    FROM (SELECT DISTINCT zip3 FROM weights) z
    CROSS JOIN generate_series(DATE '{e["grid_start"]}', DATE '{e["grid_end"]}',
                               INTERVAL 1 MONTH) AS m(month)
),
{",".join(hits)},
filled AS (
    SELECT zip3, month,
    {cols}
    FROM grid
    {joins}
)
SELECT *,
    {exposure}
FROM filled
ORDER BY zip3, month
"""


def _copy(con: duckdb.DuckDBPyConnection, query: str, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    try:
        con.execute(f"COPY ({query}) TO {_sql_str(tmp)} (FORMAT PARQUET, COMPRESSION ZSTD)")
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)


def summary_markdown(con: duckdb.DuckDBPyConnection, loans_path: Path | None) -> str:
    def table(query: str) -> str:
        cur = con.execute(query)
        headers = [d[0] for d in cur.description]
        rows = cur.fetchall()
        lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
        lines += ["| " + " | ".join(str(v) for v in r) + " |" for r in rows]
        return "\n".join(lines)

    sections = [
        "# Climate data summary",
        "Generated by `python -m climrisk.features.climate`. Aggregate statistics only.",
        "## Coverage",
        table("""SELECT count(DISTINCT zip3) AS zip3_with_weights,
                        count(*) AS zip3_county_pairs,
                        round(max(abs(total - 1)), 9) AS max_weight_sum_error
                 FROM weights
                 JOIN (SELECT zip3, sum(weight) AS total FROM weights GROUP BY zip3)
                   USING (zip3)"""),
        "## Qualifying county-disasters not matched to a ZIP3 (by definition)",
        table("""SELECT 'main' AS definition, count(*) AS county_disasters,
                        count(*) FILTER (WHERE county_fips NOT IN (SELECT county_fips FROM weights))
                            AS unmatched
                 FROM events_main
                 UNION ALL
                 SELECT 'broad', count(*),
                        count(*) FILTER (WHERE county_fips NOT IN (SELECT county_fips FROM weights))
                 FROM events_broad"""),
        "## Unmatched county-disasters by state FIPS",
        table("""SELECT definition, substr(county_fips, 1, 2) AS state_fips,
                        count(*) AS county_disasters
                 FROM (SELECT 'main' AS definition, * FROM events_main
                       UNION ALL SELECT 'broad', * FROM events_broad)
                 WHERE county_fips NOT IN (SELECT county_fips FROM weights)
                 GROUP BY 1, 2 ORDER BY 1, 3 DESC, 2"""),
        "## County-disasters by year (main definition)",
        table("""SELECT year(month) AS year, count(*) AS county_disasters,
                        count(DISTINCT disaster_number) AS disasters
                 FROM events_main WHERE month >= DATE '2000-01-01' GROUP BY 1 ORDER BY 1"""),
        "## Sanity check: most affected ZIP3, August-September 2017 (Harvey, Irma)",
        table("""SELECT zip3, month, round(hit_share_main, 3) AS hit_share_main
                 FROM climate WHERE month IN (DATE '2017-08-01', DATE '2017-09-01')
                 QUALIFY row_number() OVER (
                     PARTITION BY month ORDER BY hit_share_main DESC, zip3) <= 10
                 ORDER BY month, hit_share_main DESC, zip3"""),
        "## Houston and Florida checks, August-September 2017",
        table("""SELECT CASE WHEN zip3 BETWEEN '770' AND '775' THEN 'Houston 770-775'
                              ELSE 'Florida 320-349' END AS region,
                        month, count(*) AS zip3_areas,
                        round(avg(hit_share_main), 3) AS mean_hit_share,
                        round(max(hit_share_main), 3) AS max_hit_share
                 FROM climate
                 WHERE (zip3 BETWEEN '770' AND '775' OR zip3 BETWEEN '320' AND '349')
                   AND month IN (DATE '2017-08-01', DATE '2017-09-01')
                 GROUP BY 1, 2 ORDER BY 1, 2"""),
        "## Ex-ante 10-year exposure across ZIP3 (main definition)",
        table("""SELECT month,
                        round(quantile_cont(exposure_main, 0.5), 2) AS p50,
                        round(quantile_cont(exposure_main, 0.9), 2) AS p90,
                        round(max(exposure_main), 2) AS max
                 FROM climate
                 WHERE month IN (DATE '2005-01-01', DATE '2015-01-01', DATE '2025-01-01')
                 GROUP BY 1 ORDER BY 1"""),
    ]
    if loans_path is not None and loans_path.exists():
        sections += [
            "## Freddie Mac loans whose ZIP3 has no weights",
            table(f"""SELECT count(*) AS loans,
                             round(100.0 * count(*) /
                                 (SELECT count(*) FROM read_parquet({_sql_str(loans_path)})), 4)
                                 AS pct_all_loans,
                             count(*) FILTER (WHERE in_pd_sample) AS in_pd_sample,
                             string_agg(DISTINCT zip3, ', ' ORDER BY zip3) AS zip3
                      FROM read_parquet({_sql_str(loans_path)})
                      WHERE zip3 IS NOT NULL
                        AND zip3 NOT IN (SELECT zip3 FROM weights)"""),
        ]
    return "\n\n".join(sections) + "\n"


def run(
    geo_dir: Path = GEO_DIR,
    fema_path: Path = FEMA_PATH,
    out_dir: Path = PROCESSED_DIR,
    report_path: Path = REPORT_PATH,
    cfg: dict | None = None,
) -> Path:
    cfg = cfg or load_config()
    con = duckdb.connect()
    con.execute(f"CREATE TABLE weights AS {weights_query(geo_dir, cfg)}")
    for d in DEFINITIONS:
        con.execute(f"CREATE TABLE events_{d} AS {events_query(cfg, d, fema_path)}")
    con.execute(f"CREATE TABLE climate AS {climate_query(cfg)}")
    bad_weights = con.execute("""
        SELECT count(*) FROM (
            SELECT zip3 FROM weights GROUP BY zip3 HAVING abs(sum(weight) - 1) > 1e-9
        )
    """).fetchone()[0]
    bad_shares = con.execute("""
        SELECT count(*) FROM climate
        WHERE hit_share_main NOT BETWEEN -1e-9 AND 1.000000001
           OR hit_share_broad NOT BETWEEN -1e-9 AND 1.000000001
    """).fetchone()[0]
    if bad_weights or bad_shares:
        con.close()
        raise ValueError("Invalid normalized weights or affected housing shares")

    _copy(
        con,
        "SELECT * FROM weights ORDER BY zip3, county_fips",
        out_dir / "zip3_county_weights.parquet",
    )
    target = out_dir / "zip3_month_climate.parquet"
    _copy(con, "SELECT * FROM climate", target)

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(summary_markdown(con, out_dir / "loans.parquet"), encoding="utf-8")
    n = con.execute("SELECT count(*) FROM climate").fetchone()[0]
    log.info("Wrote %s ZIP3-month rows to %s; summary in %s", f"{n:,}", target, report_path)
    con.close()
    return target


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()


if __name__ == "__main__":
    main()
