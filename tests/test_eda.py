"""Synthetic checks for EDA grouping, annual coverage and figure generation."""

import duckdb
import pytest

from climrisk.viz import eda


@pytest.fixture
def processed(tmp_path):
    with duckdb.connect() as con:
        con.execute("""
            CREATE TABLE climate AS
            SELECT zip3, CAST(m AS DATE) AS month,
              (zip3 IN ('770', '006') AND m = DATE '2017-08-01')::INT AS hit_share_main,
              ((zip3 IN ('770', '006') AND m = DATE '2017-08-01') OR
               (zip3 = '762' AND m = DATE '2017-09-01'))::INT AS hit_share_broad,
              0.0 AS exposure_main
            FROM (VALUES ('770'), ('750'), ('762'), ('006')) z(zip3),
              generate_series(DATE '2016-08-01', DATE '2019-08-01', INTERVAL 1 MONTH) g(m)
        """)
        con.execute("""
            CREATE TABLE panel AS SELECT zip3, month AS period, 1000 AS n_loans,
              CASE WHEN zip3 = '770' AND month = DATE '2017-10-01' THEN 80 ELSE 10 END
                AS n_dlq60,
              CASE WHEN zip3 = '770' AND month = DATE '2017-09-01' THEN 50 ELSE 0 END
                AS n_disaster_dlq FROM climate
        """)
        con.execute("""
            CREATE TABLE weights AS SELECT * FROM
              (VALUES ('770', '48201', 1000.0, 1.0), ('750', '48113', 800.0, 1.0),
                      ('762', '48439', 500.0, 1.0), ('006', '72001', 100.0, 1.0))
              t(zip3, county_fips, housing, weight)
        """)
        con.execute("""
            CREATE TABLE loans AS SELECT 2005 + i % 10 AS vintage,
              (i % 37 = 0)::TINYINT AS default_24m, false AS overlaps_covid,
              true AS in_pd_sample, 600 + i % 220 AS fico, 50 + i % 47 AS ltv,
              '770' AS zip3, make_date(2005 + i % 10, 3, 1) AS first_payment_date
            FROM range(3000) t(i)
        """)
        for table, name in [
            ("climate", "zip3_month_climate"),
            ("panel", "zip3_month"),
            ("weights", "zip3_county_weights"),
            ("loans", "loans"),
        ]:
            con.execute(f"COPY {table} TO '{tmp_path / name}.parquet' (FORMAT PARQUET)")
        con.execute(f"""COPY (SELECT 1 AS disaster_number, '48201' AS county_fips,
            DATE '2017-08-23' AS incident_begin, 'DR' AS declaration_type,
            'Hurricane' AS incident_type, false AS statewide, true AS household_assistance)
            TO '{tmp_path / "fema.parquet"}' (FORMAT PARQUET)""")
    return tmp_path


def test_bin_labels_keep_their_order():
    with duckdb.connect() as con:
        case = eda.bin_case("x", [620, 680])
        assert [
            con.sql(f"SELECT {case} FROM (SELECT {v} AS x)").fetchone()[0] for v in (600, 650, 700)
        ] == ["1: <620", "2: 620-679", "3: >=680"]


def test_harvey_groups(processed):
    with duckdb.connect() as con:
        for view, name in [("climate", "zip3_month_climate"), ("panel", "zip3_month")]:
            con.execute(f"CREATE VIEW {view} AS FROM '{processed / name}.parquet'")
        rows = eda.harvey_series(con, eda.load_config())
    assert {r[0]: r[5] for r in rows} == {
        "Houston area (hit by Harvey)": 1,
        "Rest of Texas (not hit)": 1,
    }
    assert next(r[2] for r in rows if r[0].startswith("Houston") and r[1] == 2) == 8
    assert min(r[1] for r in rows) == -12 and max(r[1] for r in rows) == 24


def test_annual_coverage_unions_counties_across_months():
    with duckdb.connect() as con:
        con.execute(
            "CREATE TABLE weights AS SELECT * FROM (VALUES ('770', 'a', 50, 0.5),"
            "('770', 'b', 50, 0.5)) w(zip3, county_fips, housing, weight)"
        )
        con.execute("CREATE TABLE climate AS SELECT DATE '2017-01-01' AS month")
        con.execute(
            "CREATE TABLE events AS SELECT * FROM (VALUES ('a', DATE '2017-01-01'),"
            "('a', DATE '2017-02-01'), ('b', DATE '2017-03-01')) e(county_fips, month)"
        )
        assert eda.disasters_by_year(con) == [(2017, 100.0)]


def test_run_writes_all_figures_and_tables(processed, tmp_path):
    report = eda.run(
        processed, tmp_path / "figures", tmp_path / "eda.md", fema_path=processed / "fema.parquet"
    )
    assert sorted(p.name for p in (tmp_path / "figures").glob("*.png")) == [
        "fig1_default_by_vintage.png",
        "fig2_disasters_by_year.png",
        "fig3_harvey_delinquency.png",
        "fig4_default_by_fico_ltv.png",
        "fig5_default_by_exposure.png",
    ]
    assert report.read_text().count("![") == 5
    assert "| vintage |" in report.read_text()


def test_vintage_rates_separate_labelled_and_pd_populations():
    with duckdb.connect() as con:
        con.execute("""
            CREATE TABLE loans AS SELECT * FROM (VALUES
                (2007, 1, false, true), (2007, 0, false, true),
                (2007, 1, false, false), (2007, NULL, false, false),
                (2020, 0, true, false))
                l(vintage, default_24m, overlaps_covid, in_pd_sample)
        """)
        rows = eda.default_by_vintage(con)
    assert rows[0][1:3] == (3, 2)
    assert rows[0][3] == pytest.approx(200 / 3)
    assert rows[0][5:] == (2, 50.0)
    assert rows[1][5:] == (0, None)
