"""Tests for the loss-given-default measurement (Phase 8)."""

import duckdb
import pandas as pd
import pytest

from climrisk.risk import lgd

TARGET_CFG = {
    "performance_cutoff": "2026-03-01",
    "loss_zero_balance_codes": ["02", "03", "09"],
    "prepay_zero_balance_codes": ["01"],
    "max_internal_gap_months": 2,
}
CFG = {
    "lgd": {
        "follow_up_months": 60,
        "flag_lookback_months": 2,
        "flag_era_start": "2014-01-01",
        "default_year_groups": {"2009-2012": [2009, 2012], "2013-2016": [2013, 2016]},
        "ltv_bins": [0, 80, 200],
    }
}
D = pd.Timestamp("2015-03-01")


def _m(k: int, base: pd.Timestamp = D):
    return (base + pd.DateOffset(months=k)).date()


def _write(df: pd.DataFrame, path) -> None:
    con = duckdb.connect()
    con.register("df", df)
    con.execute(f"COPY (SELECT * FROM df) TO '{path}' (FORMAT PARQUET)")


@pytest.fixture
def data(tmp_path):
    ids = ["L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8", "L9", "L10", "L11"]
    early = pd.Timestamp("2010-06-01")
    late = pd.Timestamp("2023-01-01")
    loans = pd.DataFrame(
        {
            "loan_id": ids,
            "vintage": 2014,
            "ltv": [70, 85, 70, 70, 70, 70, 70, 95, 70, 70, 70],
            "orig_upb": 150_000,
            "default_24m": 1,
            "in_pd_sample": True,
            "default_date": [D.date()] * 6 + [late.date(), D.date(), early.date()] + [D.date()] * 2,
            "exit_code": ["09", "01", None, None, "16", "03", None, None, None, None, None],
            "exit_date": [_m(30), _m(10), None, None, _m(20), _m(25)] + [None] * 5,
        }
    )
    rows = []

    def add(loan, k, status="03", upb=100_000.0, base=D, **extra):
        rows.append(
            {
                "loan_id": loan,
                "period": _m(k, base),
                "delinquency_status": status,
                "current_upb": upb,
                **extra,
            }
        )

    for loan in ("L1", "L3", "L4", "L5", "L6"):
        add(loan, 0)
    add("L2", 0, upb=200_000.0)
    add("L8", 0, upb=0.0)  # balance masked in the first months -> original balance
    add("L9", 0, base=early)
    add("L9", -1, base=early, disaster_delinquency="Y")  # before 2014: no default type
    add("L9", 60, status="00", base=early)
    add("L3", -1, disaster_delinquency="Y")  # disaster flag one month before default
    add("L1", 31, status="RA", zero_balance_code="09", actual_loss=50_000.0)
    add("L2", 10, status="00", zero_balance_code="01")
    add("L3", 30, status="00", cum_modification_cost=5_000.0)
    add("L3", 60, status="00", cum_modification_cost=5_000.0)
    add("L4", 60, status="06")
    add("L5", 20, zero_balance_code="16")
    add("L6", 25, zero_balance_code="03")  # loss exit, loss not reported
    add("L8", 60, status="00")
    add("L10", 0)
    add("L10", 30, status="00")  # records stop at D+30 with no exit: outcome unknown
    add("L11", 0)
    add("L11", 59, status="00")  # last record at D+59: within the tolerated 2-month gap
    perf = pd.DataFrame(rows)
    for col in ("zero_balance_code", "disaster_delinquency", "assistance_plan"):
        if col not in perf:
            perf[col] = None
        perf[col] = perf[col].astype("string")
    for col in ("actual_loss", "cum_modification_cost"):
        perf[col] = perf[col].astype(float)
    for sub in ("origination", "performance"):
        (tmp_path / "interim" / sub).mkdir(parents=True)
    _write(perf, tmp_path / "interim" / "performance" / "2014.parquet")
    _write(pd.DataFrame({"loan_id": ids}), tmp_path / "interim" / "origination" / "2014.parquet")
    (tmp_path / "processed").mkdir()
    (tmp_path / "reports").mkdir()
    _write(loans, tmp_path / "processed" / "loans.parquet")
    return tmp_path


def test_resolution_ead_and_lgd(data):
    out = lgd.run(data / "processed", data / "interim", data / "reports", CFG, TARGET_CFG)
    df = duckdb.sql(f"SELECT * FROM '{out}'").df().set_index("loan_id")
    assert "L7" not in df.index  # default in 2023: no 60-month follow-up
    assert df["resolution"].to_dict() == {
        "L1": "loss",
        "L2": "paid_off",
        "L3": "cured",
        "L4": "still_delinquent",
        "L5": "transferred",
        "L6": "loss_unreported",
        "L8": "cured",
        "L9": "cured",
        "L10": "incomplete_follow_up",
        "L11": "cured",
    }
    assert df.loc["L1", "loss"] == 50_000 and df.loc["L2", "loss"] == 0
    assert df.loc["L8", "ead"] == 150_000  # masked balance -> original balance
    assert df.loc["L3", "default_type"] == "disaster-flagged"
    assert df.loc["L1", "default_type"] == "neither flag"
    assert pd.isna(df.loc["L9", "default_type"])  # defaulted before the flag existed
    assert df.loc["L3", "mod_cost"] == 5_000
    assert not df.loc[["L4", "L5", "L10"], "lgd_resolved"].any()
    # unpublished loss exit: EAD x severity of the period's reported loss exits (50%)
    assert df.loc["L6", "lgd_resolved"] and df.loc["L6", "loss"] == pytest.approx(50_000)
    assert df.loc["L4", "loss_low"] == 0 and df.loc["L4", "loss_high"] == pytest.approx(50_000)

    con = duckdb.connect()
    row = {r[0]: r for r in lgd.group_rows(con, out, "default_period")}["2013-2016"]
    # resolved: L1 50k/100k, L2 0/200k, L3 0/100k, L6 50k/100k, L8 0/150k, L11 0/100k;
    # unresolved: L4, L5, L10 (100k each)
    assert row[4] == pytest.approx(50.0)  # severity of the reported loss exit
    assert row[6] == pytest.approx(100 * 100_000 / 750_000)  # LGD over resolved
    assert row[5] == pytest.approx(100 * 100_000 / 1_050_000)  # low: unresolved lose nothing
    assert row[7] == pytest.approx(100 * 250_000 / 1_050_000)  # high: unresolved lose 50%
