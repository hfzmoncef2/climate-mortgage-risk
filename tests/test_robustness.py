"""Synthetic checks for disaster heterogeneity and default follow-up."""

import duckdb
import pandas as pd
import pytest

from climrisk.causal import robustness
from climrisk.features import climate, target
from climrisk.ingest import fema


def test_subset_keeps_events_and_their_stack_controls():
    df = pd.DataFrame(
        {
            "stack": ["2010-06"] * 3 + ["2012-09"] * 2,
            "zip3": ["770", "771", "750", "330", "331"],
            "treated": [1, 1, 0, 1, 0],
        }
    )
    keep = pd.DataFrame({"zip3": ["770"], "stack": ["2010-06"]})
    assert sorted(robustness.subset(df, keep).zip3) == ["750", "770"]


def test_event_type_is_the_group_with_most_homes(tmp_path):
    header = (
        "disasterNumber,declarationType,incidentType,state,fipsStateCode,fipsCountyCode,"
        "declarationDate,incidentBeginDate,incidentEndDate,iaProgramDeclared,"
        "ihProgramDeclared,paProgramDeclared,declarationTitle,designatedArea"
    )
    # Repeated floods in the same county must not outweigh Houston's housing share.
    rows = [
        ("1", "Hurricane", "201", "2017-08-25"),
        ("2", "Flood", "157", "2017-08-28"),
        ("4", "Flood", "157", "2017-08-28"),
        ("5", "Flood", "157", "2017-08-28"),
        ("3", "Fire", "201", "2017-09-10"),
    ]
    lines = [header] + [
        f"{n},DR,{t},TX,48,{c},{d}T00:00:00.000Z,{d}T00:00:00.000Z,,0,1,1,T,A"
        for n, t, c, d in rows
    ]
    (tmp_path / "fema.csv").write_text("\n".join(lines) + "\n")
    fema.parse(tmp_path / "fema.csv", tmp_path / "decl.parquet")
    with duckdb.connect() as con:
        con.execute("""CREATE VIEW weights AS SELECT * FROM (VALUES
            ('770', '48201', 200.0, 2.0/3), ('770', '48157', 100.0, 1.0/3))
            t(zip3, county_fips, housing, weight)""")
        types = {
            (z, s): g
            for z, s, g in con.execute(
                robustness.event_types_query(
                    climate.load_config(), robustness.load_config(), tmp_path / "decl.parquet"
                )
            ).fetchall()
        }
    assert types["770", "2017-08"] == "Hurricane / tropical storm"
    assert types["770", "2017-09"] == "Fire"


@pytest.fixture
def cure_data(tmp_path):
    for sub in ("origination", "performance"):
        (tmp_path / "interim" / sub).mkdir(parents=True)
    with duckdb.connect() as con:
        con.execute(
            f"COPY (SELECT 'X' AS loan_id) TO '{tmp_path}/interim/origination/2015.parquet'"
        )
        con.execute("""CREATE TABLE perf (loan_id VARCHAR, period DATE,
            delinquency_status VARCHAR, zero_balance_code VARCHAR,
            disaster_delinquency VARCHAR, assistance_plan VARCHAR, zero_balance_date DATE)""")
        rows = [
            ("L1", "2015-01-01", "03", None, "Y", None, None),
            ("L1", "2015-06-01", "00", None, None, None, None),
            ("L2", "2015-01-01", "03", None, None, None, None),
            ("L2", "2015-10-01", "05", "09", None, None, None),
            ("L3", "2014-12-01", "02", None, None, "F", None),
            ("L3", "2015-01-01", "03", None, None, "F", None),
            ("L3", "2017-01-01", "10", None, None, None, None),
            ("L4", "2020-05-01", "03", None, "Y", "F", None),
            ("L5", "2013-03-01", "03", None, None, None, None),
        ]
        con.executemany("INSERT INTO perf VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        con.execute(f"COPY perf TO '{tmp_path}/interim/performance/2015.parquet'")
        con.execute(f"""COPY (SELECT * FROM (VALUES
            ('L1', 'default', DATE '2015-01-01'), ('L2', 'default', DATE '2015-01-01'),
            ('L3', 'default', DATE '2015-01-01'), ('L4', 'default', DATE '2020-05-01'),
            ('L5', 'default', DATE '2013-03-01'), ('L6', 'performing', NULL))
            t(loan_id, outcome, default_date)) TO '{tmp_path}/loans.parquet'""")
    return tmp_path


def test_cure_analysis(cure_data):
    with duckdb.connect() as con:
        target.register_views(con, cure_data / "interim")
        rows = {
            r[0]: r[1:]
            for r in con.execute(
                robustness.cure_query(
                    target.load_config(), robustness.load_config(), cure_data / "loans.parquet"
                )
            ).fetchall()
        }
    assert rows["Flagged due to disaster"] == (1, 0.0, 100.0, 0.0, 0.0, 0.0, 0.0)
    assert rows["Neither flag"] == (1, 100.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    assert rows["Forbearance, not disaster"] == (1, 0.0, 0.0, 100.0, 0.0, 0.0, 0.0)
    assert sum(r[0] for r in rows.values()) == 3


def test_missing_followup_is_not_still_delinquent(cure_data):
    with duckdb.connect() as con:
        target.register_views(con, cure_data / "interim")
        con.execute("CREATE TABLE observed AS SELECT * FROM perf WHERE period < '2017-01-01'")
        con.execute("CREATE OR REPLACE VIEW perf AS SELECT * FROM observed")
        rows = {
            r[0]: r[1:]
            for r in con.execute(
                robustness.cure_query(
                    target.load_config(), robustness.load_config(), cure_data / "loans.parquet"
                )
            ).fetchall()
        }
    assert rows["Forbearance, not disaster"] == (1, 0.0, 0.0, 0.0, 0.0, 100.0, 0.0)


def test_small_subsamples_are_not_estimated():
    df = pd.DataFrame({"zip3": ["770"], "stack": ["2010-06"], "treated": [1]})
    assert robustness.summarise("Small", df, {}, min_events=10) is None


def test_late_reported_loss_uses_exit_date_and_overrides_cure(cure_data):
    with duckdb.connect() as con:
        target.register_views(con, cure_data / "interim")
        con.execute("CREATE TABLE observed AS SELECT * FROM perf")
        con.execute("""INSERT INTO observed VALUES
            ('L1', DATE '2017-03-01', '00', '09', NULL, NULL, DATE '2016-12-01')""")
        con.execute("CREATE OR REPLACE VIEW perf AS SELECT * FROM observed")
        rows = {
            r[0]: r[1:]
            for r in con.execute(
                robustness.cure_query(
                    target.load_config(), robustness.load_config(), cure_data / "loans.parquet"
                )
            ).fetchall()
        }
    assert rows["Flagged due to disaster"] == (1, 100.0, 0.0, 0.0, 0.0, 0.0, 0.0)


@pytest.mark.parametrize("terminal_status, expected_current", [("00", 100.0), ("03", 0.0)])
def test_ever_cured_is_distinct_from_current_at_end(cure_data, terminal_status, expected_current):
    with duckdb.connect() as con:
        target.register_views(con, cure_data / "interim")
        con.execute("CREATE TABLE observed AS SELECT * FROM perf")
        con.execute(
            "INSERT INTO observed VALUES ('L1', DATE '2017-01-01', ?, NULL, NULL, NULL, NULL)",
            [terminal_status],
        )
        con.execute("CREATE OR REPLACE VIEW perf AS SELECT * FROM observed")
        rows = {
            r[0]: r[1:]
            for r in con.execute(
                robustness.cure_query(
                    target.load_config(), robustness.load_config(), cure_data / "loans.parquet"
                )
            ).fetchall()
        }
    assert rows["Flagged due to disaster"][2] == 100.0  # Ever cured in June 2015.
    assert rows["Flagged due to disaster"][-1] == expected_current
