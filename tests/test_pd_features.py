"""Tests for the PD feature table (Phase 7)."""

import math

import duckdb
import pandas as pd
import pytest

from climrisk.features import pd_features

CFG = {
    "split": {"train": [2004, 2014], "validation": [2015, 2018], "test": [2021, 2024]},
    "features": {
        "numeric": [
            "fico",
            "ltv",
            "cltv",
            "dti",
            "log_upb",
            "orig_term",
            "mi_pct",
            "num_borrowers",
            "num_units",
            "spread_bp",
        ],
        "categorical": [
            "loan_purpose",
            "occupancy",
            "channel",
            "property_type",
            "first_time_buyer",
            "super_conforming",
            "state",
        ],
        "climate": [
            "exposure_main",
            "exposure_broad",
            "recent_events_12m",
            "near_coast_share",
            "log_dist_coast",
        ],
        "recent_months": 12,
    },
}
CLIMATE_CFG = {"exposure": {"availability_lag_months": 3}}


def _write(df: pd.DataFrame, path) -> None:
    con = duckdb.connect()
    con.register("df", df)
    con.execute(f"COPY (SELECT * FROM df) TO '{path}' (FORMAT PARQUET)")


@pytest.fixture
def data(tmp_path):
    loans = pd.DataFrame(
        {
            "loan_id": ["A", "B", "C", "D"],
            "vintage": [2010, 2016, 2022, 2010],
            "first_payment_date": pd.to_datetime(
                ["2010-03-01", "2016-03-01", "2022-03-01", "2010-03-01"]
            ).date,
            "zip3": ["331", "331", "802", "008"],
            "in_pd_sample": [True, True, True, False],
            "default_24m": [1, 0, 0, 0],
            "fico": [700, None, 760, 700],
            "ltv": 80,
            "cltv": 80,
            "dti": 35,
            "orig_upb": 200_000.0,
            "orig_term": 360,
            "mi_pct": 0,
            "num_borrowers": 2,
            "num_units": 1,
            "orig_rate": 5.5,
            "loan_purpose": "P",
            "occupancy": "P",
            "channel": "R",
            "property_type": "SF",
            "first_time_buyer": [None, "N", "N", "N"],
            "super_conforming": "N",
            "state": ["FL", "FL", "CO", "VI"],
            "eltv": 99,  # performance-file field: must not reach the features
        }
    )
    months = pd.date_range("2008-01-01", "2022-12-01", freq="MS")
    clim = pd.DataFrame([(z, m) for z in ["331", "802"] for m in months], columns=["zip3", "month"])
    clim["events_main"] = 0.0
    # Origination of A = 2010-01. With a 3-month lag the 12-month window is 2008-10..2009-09.
    clim.loc[(clim.zip3 == "331") & (clim.month == "2009-09-01"), "events_main"] = 1.0  # in
    clim.loc[(clim.zip3 == "331") & (clim.month == "2008-10-01"), "events_main"] = 0.5  # in
    clim.loc[(clim.zip3 == "331") & (clim.month == "2009-10-01"), "events_main"] = 9.0  # too recent
    clim.loc[(clim.zip3 == "331") & (clim.month == "2008-09-01"), "events_main"] = 9.0  # too old
    clim["exposure_main"] = 1.0
    clim["exposure_broad"] = 2.0
    clim["month"] = clim["month"].dt.date
    _write(loans, tmp_path / "loans.parquet")
    _write(clim, tmp_path / "zip3_month_climate.parquet")
    _write(
        pd.DataFrame(
            {"zip3": ["331", "802"], "near_coast_share": [1.0, 0.0], "dist_coast_km": [0.0, 1428.8]}
        ),
        tmp_path / "zip3_coast.parquet",
    )
    _write(pd.DataFrame({"month": months.date, "pmms_rate": 5.0}), tmp_path / "pmms.parquet")
    return tmp_path


def test_features_are_ex_ante_and_split_by_vintage(data):
    path = pd_features.run(data, data / "pmms.parquet", CFG, CLIMATE_CFG, origination_lag=2)
    df = duckdb.sql(f"SELECT * FROM '{path}' ORDER BY loan_id").df().set_index("loan_id")
    assert list(df.index) == ["A", "B", "C"]  # D is outside the PD sample
    assert df["split"].to_dict() == {"A": "train", "B": "validation", "C": "test"}
    assert df.loc["A", "recent_events_12m"] == pytest.approx(1.5)
    assert df.loc["A", "spread_bp"] == pytest.approx(50.0)
    assert df.loc["C", "log_dist_coast"] == pytest.approx(math.log(1 + 1428.8))
    assert df.loc["A", "first_time_buyer"] == "missing"
    assert pd.isna(df.loc["B", "fico"])  # missing numeric stays missing
    base, clim = pd_features.feature_columns(CFG)
    assert set(base + clim) <= set(df.columns)
    assert "eltv" not in df.columns and "vintage" not in base
