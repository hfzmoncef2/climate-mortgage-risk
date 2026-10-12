"""Physical hurricane-coast exposure by county and ZIP3 (Phase 6).

A static, retrospective physical measure (D016): distance from each county to the
Atlantic / Gulf of Mexico coastline (Census TIGER 2020), aggregated to ZIP3 with the
2010 housing weights.

Outputs:
    data/interim/geo/county_coast.parquet  (county_fips [2010 codes], dist_coast_km)
    data/processed/zip3_coast.parquet      (zip3, dist_coast_km, shoreline_share,
                                            near_coast_share, coverage)

Usage:
    uv run --frozen python -m climrisk.features.coast
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import duckdb
import geopandas as gpd
import pandas as pd
import yaml

from climrisk.features import climate
from climrisk.features.target import PROCESSED_DIR
from climrisk.ingest.download import download
from climrisk.ingest.freddie import ROOT, _sql_str

CONFIG_PATH = ROOT / "configs" / "pricing.yaml"
RAW_DIR = ROOT / "data" / "raw" / "geo"
COUNTY_OUT = ROOT / "data" / "interim" / "geo" / "county_coast.parquet"

log = logging.getLogger(__name__)


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def county_distances(
    counties: gpd.GeoDataFrame, coastline: gpd.GeoDataFrame, cfg: dict, recode: dict
) -> pd.DataFrame:
    """Distance (km) from each county polygon to the hurricane coastline.

    County codes created after 2010 are mapped back to their 2010 code (same mapping as
    configs/climate.yaml); when several 2020 counties map to one 2010 code, the minimum
    distance is kept.
    """
    c = cfg["coast"]
    epsg = int(c["projection_epsg"])
    coast = coastline[coastline["NAME"].isin(c["hurricane_coasts"])].to_crs(epsg)
    if coast.empty:
        raise ValueError("No coastline matches the configured hurricane coast names")
    line = coast.geometry.union_all()
    projected = counties.to_crs(epsg)
    out = pd.DataFrame(
        {
            "county_fips": projected["GEOID"].map(lambda g: recode.get(g, g)),
            "dist_coast_km": projected.geometry.distance(line) / 1000.0,
        }
    )
    out = out.groupby("county_fips", as_index=False)["dist_coast_km"].min()
    # 2010 counties dissolved before 2020 (e.g. Bedford city, VA) take their parent's value
    parents = c.get("dissolved_2010_counties") or {}
    by_code = out.set_index("county_fips")["dist_coast_km"]
    extra = pd.DataFrame(
        [(old, by_code[new]) for old, new in parents.items() if new in by_code.index],
        columns=["county_fips", "dist_coast_km"],
    )
    return pd.concat([out, extra], ignore_index=True)


def zip3_query(weights_path: Path, county_path: Path, cfg: dict) -> str:
    """Housing-weighted ZIP3 aggregates. Counties without a distance (no 2020 shape) are
    dropped and the remaining weights renormalised; `coverage` reports the kept share."""
    c = cfg["coast"]
    return f"""
SELECT
    w.zip3,
    sum(w.weight * d.dist_coast_km) / sum(w.weight)                        AS dist_coast_km,
    sum(w.weight * (d.dist_coast_km <= {c["shoreline_km"]})::INT) / sum(w.weight)
                                                                           AS shoreline_share,
    sum(w.weight * (d.dist_coast_km <= {c["near_coast_km"]})::INT) / sum(w.weight)
                                                                           AS near_coast_share,
    CAST(sum(w.weight) AS DOUBLE)                                          AS coverage
FROM read_parquet({_sql_str(weights_path)}) w
JOIN read_parquet({_sql_str(county_path)}) d USING (county_fips)
GROUP BY w.zip3
ORDER BY w.zip3
"""


def run(
    counties: gpd.GeoDataFrame | None = None,
    coastline: gpd.GeoDataFrame | None = None,
    processed_dir: Path = PROCESSED_DIR,
    county_out: Path = COUNTY_OUT,
    cfg: dict | None = None,
    recode: dict | None = None,
) -> Path:
    cfg = cfg or load_config()
    if recode is None:
        recode = climate.load_config().get("county_recode") or {}
    if counties is None or coastline is None:
        s = cfg["sources"]
        county_zip = download(s["counties_url"], RAW_DIR / Path(s["counties_url"]).name)
        coast_zip = download(s["coastline_url"], RAW_DIR / Path(s["coastline_url"]).name)
        counties = gpd.read_file(f"zip://{county_zip}")
        coastline = gpd.read_file(f"zip://{coast_zip}")

    dist = county_distances(counties, coastline, cfg, recode)
    county_out.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.register("dist", dist)
    county_tmp = county_out.with_name(county_out.name + ".tmp")
    try:
        con.execute(
            f"COPY (SELECT * FROM dist ORDER BY county_fips) "
            f"TO {_sql_str(county_tmp)} (FORMAT PARQUET)"
        )
        county_tmp.replace(county_out)
    finally:
        county_tmp.unlink(missing_ok=True)
    log.info(
        "County distances: %d counties, %d within %s km",
        len(dist),
        int((dist["dist_coast_km"] <= cfg["coast"]["shoreline_km"]).sum()),
        cfg["coast"]["shoreline_km"],
    )

    processed_dir.mkdir(parents=True, exist_ok=True)
    target = processed_dir / "zip3_coast.parquet"
    query = zip3_query(processed_dir / "zip3_county_weights.parquet", county_out, cfg)
    tmp = target.with_name(target.name + ".tmp")
    try:
        con.execute(f"COPY ({query}) TO {_sql_str(tmp)} (FORMAT PARQUET)")
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)
    n, low = con.execute(
        f"SELECT count(*), count(*) FILTER (WHERE coverage < 0.99) "
        f"FROM read_parquet({_sql_str(target)})"
    ).fetchone()
    log.info("Wrote %d ZIP3 (%d with coverage < 99%%) -> %s", n, low, target)
    con.close()
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="Hurricane-coast distance by county and ZIP3")
    parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()


if __name__ == "__main__":
    main()
