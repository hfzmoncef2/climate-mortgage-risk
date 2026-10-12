"""Tests for the credit-default sensitivity target (D022)."""

import duckdb
import pandas as pd
import pytest

from climrisk.features import credit_default

TARGET_CFG = {
    "performance_cutoff": "2026-03-01",
    "loss_zero_balance_codes": ["02", "03", "09"],
    "prepay_zero_balance_codes": ["01"],
    "default_status_codes": ["RA"],
}
CFG = {"credit_default": {"follow_up_months": 12}}
D = pd.Timestamp("2016-03-01")  # first default month of every defaulted loan


def _write(df: pd.DataFrame, path) -> None:
    con = duckdb.connect()
    con.register("df", df)
    con.execute(f"COPY (SELECT * FROM df) TO '{path}' (FORMAT PARQUET)")


def _month(k: int):
    return (D + pd.DateOffset(months=k)).date()


@pytest.fixture
def data(tmp_path):
    loans = pd.DataFrame(
        {
            "loan_id": ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"],
            "vintage": [2015] * 7 + [2024, 2015, 2015],
            "default_24m": [0, 1, 1, 1, 1, 1, 1, 0, 1, 1],
            "default_date": [None] + [D.date()] * 6 + [None, D.date(), D.date()],
            "window_end": [pd.Timestamp("2016-12-01").date()] * 7
            + [pd.Timestamp("2026-01-01").date()]
            + [pd.Timestamp("2016-12-01").date()] * 2,
            "exit_code": [None, None, None, "03", "01", None, None, None, None, None],
            "exit_date": [None, None, None, _month(9), _month(7), None, None, None, None, None],
            "in_pd_sample": [True] * 10,
        }
    )
    statuses = {
        "B": {12: "00"},  # back to current at D+12 -> temporary
        "C": {5: "RA"},  # REO -> credit default
        "F": {12: "05"},  # still delinquent at D+12 -> persistent
        "G": {11: "00"},  # current at D+11 but not observed at D+12 -> conservative default
        "I": {6: "RA", 12: "00"},  # REO first: a later current status does not cure it
        "J": {4: "00", 12: "00"},  # current at D+12 after a payment deferral at D+4
    }
    rows = [("B", _month(0), "03"), ("C", _month(0), "03"), ("F", _month(0), "03")]
    rows += [(loan, _month(k), s) for loan, d in statuses.items() for k, s in d.items()]
    perf = pd.DataFrame(rows, columns=["loan_id", "period", "delinquency_status"])
    for col in ("zero_balance_code", "modification_flag", "payment_deferral"):
        perf[col] = pd.Series([None] * len(perf), dtype="string")  # text, as in the real data
    perf.loc[(perf["loan_id"] == "J") & (perf["period"] == _month(4)), "payment_deferral"] = "Y"
    for sub in ("origination", "performance"):
        (tmp_path / "interim" / sub).mkdir(parents=True)
    _write(perf, tmp_path / "interim" / "performance" / "2015.parquet")
    _write(pd.DataFrame({"loan_id": ["A"]}), tmp_path / "interim" / "origination" / "2015.parquet")
    (tmp_path / "processed").mkdir()
    (tmp_path / "reports").mkdir()
    _write(loans, tmp_path / "processed" / "loans.parquet")
    return tmp_path


def test_resolution_of_each_case(data):
    out = credit_default.run(
        data / "processed", data / "interim", data / "reports", CFG, TARGET_CFG
    )
    df = duckdb.sql(f"SELECT * FROM '{out}'").df().set_index("loan_id")
    expected = {
        "A": ("no_default", 0),
        "B": ("temporary_current", 0),
        "C": ("loss_or_reo", 1),
        "D": ("loss_or_reo", 1),  # short sale / charge-off at D+9
        "E": ("temporary_payoff", 0),  # voluntary payoff at D+7
        "F": ("persistent", 1),
        "G": ("persistent", 1),
        "I": ("loss_or_reo", 1),
        "J": ("temporary_current", 0),  # still temporary: workouts do not change the target
    }
    for loan, (resolution, label) in expected.items():
        assert df.loc[loan, "resolution"] == resolution, loan
        assert df.loc[loan, "credit_default_24m"] == label, loan
    assert df.loc["J", "workout"] and not df.loc["B", "workout"]
    assert df["credit_eligible"].drop("H").all()
    assert not df.loc["H", "credit_eligible"]  # 2026-01 + 12 months > cutoff
    assert (data / "reports" / "credit_default.md").exists()
