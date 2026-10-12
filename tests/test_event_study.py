"""Stacked event study with a known +2 percentage-point effect."""

import duckdb
import pytest

from climrisk.causal import event_study


def config():
    cfg = event_study.load_config()
    cfg["outcomes"] = {"dlq60": "n_dlq60", "prepaid": "n_prepaid"}
    cfg["robustness_thresholds"] = [0.5, 0.9]
    return cfg


@pytest.fixture
def processed(tmp_path):
    with duckdb.connect() as con:
        con.execute("""
            CREATE TABLE climate AS
            SELECT zip3, CAST(m AS DATE) AS month,
                CASE WHEN zip3 IN ('770', '771', '772', '773') AND m = DATE '2010-06-01'
                       THEN 1.0
                     WHEN zip3 = '773' AND m = DATE '2009-12-01' THEN 1.0
                     WHEN zip3 = '774' AND m = DATE '2020-08-01' THEN 1.0
                     ELSE 0.0 END AS hit_share_main
            FROM (SELECT CAST(i AS VARCHAR) AS zip3 FROM range(750, 775) t(i)
                  UNION ALL SELECT '330') z,
                 generate_series(DATE '2000-01-01', DATE '2022-12-01', INTERVAL 1 MONTH) g(m)
        """)
        con.execute(f"""COPY (SELECT *, hit_share_main AS hit_share_broad FROM climate)
            TO '{tmp_path / "zip3_month_climate.parquet"}' (FORMAT PARQUET)""")
        con.execute(f"""COPY (
            SELECT DISTINCT zip3,
                   CASE WHEN zip3 = '330' THEN '12086' ELSE '48201' END AS county_fips,
                   1000.0 AS housing, 1.0 AS weight
            FROM climate) TO '{tmp_path / "zip3_county_weights.parquet"}' (FORMAT PARQUET)""")
        con.execute(f"""COPY (
            SELECT zip3, month AS period, 1000 AS n_loans,
                CAST(10 + CAST(zip3 AS INT) % 7 + 3 * (month(month) % 4)
                  + CASE WHEN zip3 IN ('770', '771', '772')
                          AND month BETWEEN DATE '2010-07-01' AND DATE '2010-12-01'
                         THEN 20 ELSE 0 END AS INT) AS n_dlq60,
                5 AS n_prepaid
            FROM climate WHERE month BETWEEN DATE '2008-01-01' AND DATE '2013-12-01')
            TO '{tmp_path / "zip3_month.parquet"}' (FORMAT PARQUET)""")
    return tmp_path


def stacked(processed):
    with duckdb.connect() as con:
        event_study.register_views(con, processed)
        return event_study.build_stacked(con, config(), 0.5)


def test_clean_events_and_controls(processed):
    df = stacked(processed)
    units = df[["stack", "zip3", "treated"]].drop_duplicates()
    treated = set(units.loc[units.treated == 1, "zip3"])
    controls = set(units.loc[units.treated == 0, "zip3"])
    assert set(units["stack"]) == {"2010-06"}
    assert treated == {"770", "771", "772"}
    assert "773" not in treated | controls
    assert "774" in controls and "330" not in controls
    assert len(controls) == 21
    assert df.rel.min() == -12 and df.rel.max() == 24


def test_recovers_the_true_effect(processed):
    result = event_study.estimate(stacked(processed), "dlq60", "n_dlq60", config())
    estimates = dict(zip(result.coefficients.rel, result.coefficients.estimate, strict=True))
    for k in range(1, 7):
        assert estimates[k] == pytest.approx(2.0, abs=1e-6)
    for k in (-12, -6, -2, 0, 7, 24):
        assert estimates[k] == pytest.approx(0, abs=1e-6)
    assert result.average_effect == pytest.approx(2, abs=1e-6)


def test_run_writes_report_and_figures(processed, tmp_path):
    reports = tmp_path / "reports"
    results = event_study.run(processed, reports, config())
    assert [r.outcome for r in results] == ["dlq60", "prepaid"]
    for name in [
        "event_study.md",
        "event_study_coefficients.csv",
        "figures/fig6_event_study_dlq60.png",
        "figures/fig7_event_study_outcomes.png",
    ]:
        assert (reports / name).exists()
