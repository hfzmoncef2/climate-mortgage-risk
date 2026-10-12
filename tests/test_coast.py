"""Check spatial units, coast selection, county mapping and partial coverage."""

import duckdb
import geopandas as gpd
import pytest
from shapely.geometry import LineString, box

from climrisk.features import coast


def test_distances_use_selected_coasts_and_merge_counties():
    cfg = coast.load_config()
    counties = gpd.GeoDataFrame(
        {"GEOID": ["46102", "02063", "02066", "51019"]},
        geometry=[
            box(10000, 0, 11000, 1000),
            box(20000, 0, 21000, 1000),
            box(5000, 0, 6000, 1000),
            box(30000, 0, 31000, 1000),
        ],
        crs=5070,
    )
    shoreline = gpd.GeoDataFrame(
        {"NAME": ["Atlantic", "Pacific"]},
        geometry=[LineString([(0, -1000), (0, 2000)]), LineString([(10000, -1000), (10000, 2000)])],
        crs=5070,
    )
    df = coast.county_distances(
        counties, shoreline, cfg, {"46102": "46113", "02063": "02261", "02066": "02261"}
    )
    distances = df.set_index("county_fips").dist_coast_km.to_dict()
    assert distances == {"46113": 10.0, "02261": 5.0, "51019": 30.0, "51515": 30.0}


def test_zip3_partial_coverage_is_reported_and_weights_renormalised(tmp_path):
    weights, counties = tmp_path / "weights.parquet", tmp_path / "counties.parquet"
    with duckdb.connect() as con:
        con.execute(f"""COPY (SELECT * FROM (VALUES
            ('770', 'a', 0.25), ('770', 'b', 0.5), ('770', 'missing', 0.25))
            w(zip3, county_fips, weight)) TO '{weights}' (FORMAT PARQUET)""")
        con.execute(f"""COPY (SELECT * FROM (VALUES ('a', 0.0), ('b', 90.0))
            d(county_fips, dist_coast_km)) TO '{counties}' (FORMAT PARQUET)""")
        row = con.execute(coast.zip3_query(weights, counties, coast.load_config())).fetchone()
    assert row[0] == "770"
    assert row[1:] == pytest.approx((60.0, 1 / 3, 1 / 3, 0.75))


def test_missing_selected_coast_fails():
    counties = gpd.GeoDataFrame({"GEOID": ["48201"]}, geometry=[box(0, 0, 1, 1)], crs=5070)
    shoreline = gpd.GeoDataFrame(
        {"NAME": ["Pacific"]}, geometry=[LineString([(0, 0), (1, 1)])], crs=5070
    )
    with pytest.raises(ValueError, match="No coastline"):
        coast.county_distances(counties, shoreline, coast.load_config(), {})
