"""Tests for Phase 6: PMMS parsing, coast distances and the pricing regressions."""

import duckdb
import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import LineString, box

from climrisk.causal import pricing
from climrisk.features import coast
from climrisk.ingest import fred


def _write(df: pd.DataFrame, path) -> None:
    con = duckdb.connect()
    con.register("df", df)
    con.execute(f"COPY (SELECT * FROM df) TO '{path}' (FORMAT PARQUET)")


# --- PMMS -----------------------------------------------------------------------------


def test_pmms_monthly_average_skips_missing_weeks(tmp_path):
    (tmp_path / "pmms.csv").write_text(
        "observation_date,MORTGAGE30US\n"
        "2017-08-03,3.93\n2017-08-10,3.90\n2017-08-17,.\n2017-08-24,3.86\n"
        "2017-09-07,3.78\n2017-10-05,.\n"
    )
    n = fred.parse(tmp_path / "pmms.csv", tmp_path / "pmms.parquet")
    rows = duckdb.sql(f"SELECT * FROM '{tmp_path}/pmms.parquet' ORDER BY month").fetchall()
    assert n == 2  # October has no valid week
    assert str(rows[0][0]) == "2017-08-01"
    assert rows[0][1] == pytest.approx((3.93 + 3.90 + 3.86) / 3)
    assert rows[1][1] == pytest.approx(3.78)


# --- Coast ----------------------------------------------------------------------------

CFG = {
    "coast": {
        "hurricane_coasts": ["Atlantic", "Gulf"],
        "projection_epsg": 5070,
        "shoreline_km": 1,
        "near_coast_km": 50,
        "dissolved_2010_counties": {"51515": "51019"},
    },
    "pricing": {
        "origination_lag_months": 2,
        "orig_term": 360,
        "bins": {"fico": 20, "ltv": 5, "dti": 5},
        "period_split": 2012,
    },
}


def test_county_distance_uses_only_hurricane_coasts(tmp_path):
    # Work directly in EPSG:5070 metres: a coastline at x = 0, a Pacific line at x = 30 km.
    coastline = gpd.GeoDataFrame(
        {"NAME": ["Atlantic", "Pacific"]},
        geometry=[LineString([(0, 0), (0, 500_000)]), LineString([(30_000, 0), (30_000, 1e5)])],
        crs=5070,
    )
    counties = gpd.GeoDataFrame(
        {"GEOID": ["12086", "46102", "51019", "08031"]},
        geometry=[
            box(-10_000, 0, 0, 10_000),  # touches the Atlantic line
            box(100_000, 0, 120_000, 10_000),  # 100 km (Pacific line at 30 km ignored)
            box(20_000, 0, 30_000, 10_000),  # 20 km
            box(1_000_000, 0, 1_010_000, 10_000),
        ],
        crs=5070,
    )
    out = coast.county_distances(counties, coastline, CFG, {"46102": "46113"})
    d = dict(zip(out["county_fips"], out["dist_coast_km"], strict=True))
    assert d["12086"] == pytest.approx(0)
    assert d["46113"] == pytest.approx(100) and "46102" not in d  # recoded to 2010 code
    assert d["51019"] == pytest.approx(20) and d["51515"] == pytest.approx(20)
    assert d["08031"] == pytest.approx(1000)

    county_path = tmp_path / "county.parquet"
    _write(out, county_path)
    _write(
        pd.DataFrame(
            {
                "zip3": ["330", "330", "802", "999"],
                "county_fips": ["12086", "46113", "08031", "99999"],
                "weight": [0.25, 0.75, 1.0, 1.0],
            }
        ),
        tmp_path / "w.parquet",
    )
    rows = duckdb.sql(coast.zip3_query(tmp_path / "w.parquet", county_path, CFG)).fetchall()
    z = {r[0]: r[1:] for r in rows}
    assert z["330"] == pytest.approx((75.0, 0.25, 0.25, 1.0))
    assert z["802"] == pytest.approx((1000.0, 0.0, 0.0, 1.0))
    assert "999" not in z  # county without a shape


# --- Pricing --------------------------------------------------------------------------

TRUE_SPREAD_COAST, TRUE_SPREAD_EXPOSURE = 12.0, 3.0  # bp
TRUE_DEFAULT_COAST = 2.0  # pp


@pytest.fixture
def pricing_data(tmp_path):
    """Synthetic loans whose spread and default depend on climate by known amounts."""
    rng = np.random.default_rng(0)
    n = 20_000
    zips = [f"{i:03d}" for i in range(100, 160)]
    zip_state = {z: ["FL", "TX", "NC"][i % 3] for i, z in enumerate(zips)}
    coast_share = {z: float(i % 4 == 0) * rng.uniform(0.5, 1) for i, z in enumerate(zips)}
    months = pd.date_range("2008-01-01", "2015-12-01", freq="MS")
    zip3 = rng.choice(zips, n)
    orig = rng.choice(months, n)
    climate_rows = pd.DataFrame(
        [(z, m, rng.uniform(0, 3)) for z in zips for m in months],
        columns=["zip3", "month", "exposure_main"],
    )
    climate_rows["month"] = climate_rows["month"].dt.date
    exposure = climate_rows.set_index(["zip3", "month"])["exposure_main"]
    fico = rng.integers(620, 820, n)
    near = np.array([coast_share[z] for z in zip3])
    exp_ = np.array(
        [exposure[(z, m.date())] for z, m in zip(zip3, pd.to_datetime(orig), strict=True)]
    )
    pmms = pd.DataFrame({"month": months.date, "pmms_rate": rng.uniform(3, 6.5, len(months))})
    pm = pmms.set_index("month")["pmms_rate"]
    spread_bp = (
        40
        + TRUE_SPREAD_COAST * near
        + TRUE_SPREAD_EXPOSURE * exp_
        - 0.2 * (fico - 700)
        + rng.normal(0, 15, n)
    )
    rate = np.array([pm[m.date()] for m in pd.to_datetime(orig)]) + spread_bp / 100
    p_default = np.clip(0.02 + TRUE_DEFAULT_COAST / 100 * near, 0, 1)
    loans = pd.DataFrame(
        {
            "loan_id": [f"F{i}" for i in range(n)],
            "vintage": pd.to_datetime(orig).year,
            "zip3": zip3,
            "state": [zip_state[z] for z in zip3],
            "first_payment_date": (pd.to_datetime(orig) + pd.DateOffset(months=2)).date,
            "orig_term": 360,
            "is_harp": False,
            "orig_rate": rate,
            "default_24m": (rng.uniform(size=n) < p_default).astype(int),
            "in_pd_sample": True,
            "fico": fico,
            "ltv": rng.integers(60, 96, n),
            "dti": rng.integers(15, 45, n),
            "orig_upb": rng.uniform(100_000, 400_000, n),
            "loan_purpose": rng.choice(["P", "C", "N"], n),
            "occupancy": "P",
            "property_type": "SF",
            "num_units": 1,
            "channel": rng.choice(["R", "B", "C"], n),
            "num_borrowers": rng.choice([1, 2], n),
            "first_time_buyer": rng.choice(["Y", "N"], n),
            "mi_pct": np.where(rng.uniform(size=n) < 0.2, 25, 0),
            "super_conforming": "N",
        }
    )
    loans.loc[0, "fico"] = None  # missing FICO becomes its own bin
    loans.loc[1, "orig_term"] = 180  # excluded
    loans.loc[2, "is_harp"] = True  # excluded
    processed = tmp_path / "processed"
    (tmp_path / "reports" / "figures").mkdir(parents=True)
    processed.mkdir()
    _write(loans, processed / "loans.parquet")
    _write(climate_rows, processed / "zip3_month_climate.parquet")
    _write(
        pd.DataFrame({"zip3": zips, "near_coast_share": [coast_share[z] for z in zips]}),
        processed / "zip3_coast.parquet",
    )
    _write(pmms, tmp_path / "pmms.parquet")
    return tmp_path


def test_loans_query_spread_and_filters(pricing_data):
    q = pricing.loans_query(CFG, pricing_data / "processed", pricing_data / "pmms.parquet")
    df = duckdb.sql(q).df()
    assert len(df) == 20_000 - 2
    assert "F1" not in set(df["loan_id"]) and "F2" not in set(df["loan_id"])
    assert df.loc[df["loan_id"] == "F0", "fico_bin"].item() == "missing"
    assert df["has_mi"].isin([0, 1]).all()
    assert df["spread_bp"].between(-100, 200).all()  # rate matched to its own PMMS month


def test_pricing_recovers_known_effects(pricing_data):
    res = pricing.run(
        processed_dir=pricing_data / "processed",
        pmms_path=pricing_data / "pmms.parquet",
        reports_dir=pricing_data / "reports",
        cfg=CFG,
    )
    est, _, lo, hi, _ = res["spread_all"]["coef"]["near_coast_share"]
    assert lo < TRUE_SPREAD_COAST < hi and est == pytest.approx(TRUE_SPREAD_COAST, abs=3)
    est, _, lo, hi, _ = res["spread_all"]["coef"]["exposure_main"]
    assert lo < TRUE_SPREAD_EXPOSURE < hi
    est, _, lo, hi, _ = res["default_pd"]["coef"]["near_coast_share"]
    assert lo < TRUE_DEFAULT_COAST < hi
    assert {"spread_early", "spread_late", "default_early", "default_late"} <= set(res)
    assert (pricing_data / "reports" / "pricing.md").exists()
    assert (pricing_data / "reports" / "figures" / "fig9_pricing_vs_default.png").exists()
