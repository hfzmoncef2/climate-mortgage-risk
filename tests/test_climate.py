"""Tests for the Census/FEMA parsers and the ZIP3 climate exposure, on synthetic data.

Geography (2010 housing units of each ZCTA x county part):
    ZIP3 770: 77001 in county 48201 (100 homes); 77002 split 48201 (100) / 48157 (100)
              -> weights 48201 = 200 / 300 = 2/3, 48157 = 100 / 300 = 1/3
    ZIP3 781: 78101 in county 48029 (50 homes), never hit
    ZIP3 577: 57701 in county 46113 (Shannon County, SD, renamed 46102 in 2015)
"""

from datetime import date

import duckdb
import pytest

from climrisk.features import climate
from climrisk.ingest import census, fema

CROSSWALK = """ZCTA5,STATE,COUNTY,GEOID,POPPT,HUPT
77001,48,201,48201,250,100
77002,48,201,48201,250,100
77002,48,157,48157,250,100
78101,48,029,48029,120,50
57701,46,113,46113,90,30
"""

FEMA_HEADER = (
    "disasterNumber,declarationType,incidentType,state,fipsStateCode,fipsCountyCode,"
    "declarationDate,incidentBeginDate,incidentEndDate,iaProgramDeclared,"
    "ihProgramDeclared,paProgramDeclared,declarationTitle,designatedArea"
)
FEMA_ROWS = [
    # number, type, incident, 5-digit county, begin, ia, ih
    ("1", "DR", "Hurricane", "48157", "2017-08-25", "0", "1"),
    ("1", "DR", "Hurricane", "48157", "2017-08-25", "0", "1"),  # duplicate row (amendment)
    ("2", "DR", "Severe Storm", "48201", "2017-08-10", "0", "0"),  # broad definition only
    ("3", "DR", "Biological", "48201", "2020-03-13", "0", "1"),  # COVID: never counted
    ("4", "DR", "Hurricane", "48000", "2017-08-25", "0", "1"),  # statewide: excluded
    ("5", "DR", "Flood", "48157", "2017-08-30", "1", "0"),  # 2nd disaster, same county-month
    ("6", "EM", "Hurricane", "48201", "2017-08-25", "0", "1"),  # emergency, not major disaster
    ("7", "DR", "Fire", "48201", "2017-09-01", "0", "1"),  # midnight UTC: must stay September
    ("8", "DR", "Fire", "46102", "2017-09-15", "0", "1"),  # post-2010 county code
]


def fema_csv() -> str:
    lines = [FEMA_HEADER]
    for number, dtype, incident, county, begin, ia, ih in FEMA_ROWS:
        ts = f"{begin}T00:00:00.000Z"
        state, cnty = county[:2], county[2:]
        lines.append(
            f"{number},{dtype},{incident},XX,{state},{cnty},{ts},{ts},,{ia},{ih},1,TITLE,AREA"
        )
    return "\n".join(lines) + "\n"


def config() -> dict:
    cfg = climate.load_config()
    cfg["exposure"] = {
        "lookback_months": 3,
        "availability_lag_months": 0,
        "grid_start": "2017-06-01",
        "grid_end": "2018-01-01",
    }
    return cfg


@pytest.fixture
def built(tmp_path):
    raw, geo = tmp_path / "raw", tmp_path / "geo"
    raw.mkdir()
    (raw / "crosswalk.txt").write_text(CROSSWALK)
    (raw / "fema.csv").write_text(fema_csv())
    census.parse(raw / "crosswalk.txt", geo / "zcta_county.parquet")
    fema.parse(raw / "fema.csv", tmp_path / "declarations.parquet")
    out = tmp_path / "processed"
    climate.run(geo, tmp_path / "declarations.parquet", out, tmp_path / "summary.md", config())
    return out


def test_fema_dates_are_not_shifted_by_time_zones(tmp_path):
    (tmp_path / "fema.csv").write_text(fema_csv())
    fema.parse(tmp_path / "fema.csv", tmp_path / "d.parquet")
    begin = duckdb.sql(
        f"SELECT incident_begin FROM read_parquet('{tmp_path / 'd.parquet'}') "
        "WHERE disaster_number = 7"
    ).fetchone()[0]
    assert begin == date(2017, 9, 1)


def test_weights(built):
    rows = duckdb.sql(
        f"SELECT zip3, county_fips, weight FROM read_parquet('{built}/zip3_county_weights.parquet')"
        " ORDER BY zip3, county_fips"
    ).fetchall()
    assert [(z, c) for z, c, _ in rows] == [
        ("577", "46113"),
        ("770", "48157"),
        ("770", "48201"),
        ("772", "48201"),
        ("781", "48029"),
    ]
    weights = {(z, c): w for z, c, w in rows}
    assert weights[("770", "48157")] == pytest.approx(1 / 3)
    assert weights[("770", "48201")] == pytest.approx(2 / 3)
    assert weights[("781", "48029")] == pytest.approx(1.0)


def climate_rows(built, zip3: str) -> dict:
    rows = duckdb.sql(
        f"SELECT month, hit_share_main, events_main, hit_share_broad, events_broad, "
        f"exposure_main FROM read_parquet('{built}/zip3_month_climate.parquet') "
        f"WHERE zip3 = '{zip3}' ORDER BY month"
    ).fetchall()
    return {r[0]: r[1:] for r in rows}


def test_disaster_calendar(built):
    rows = climate_rows(built, "770")
    aug, sep = rows[date(2017, 8, 1)], rows[date(2017, 9, 1)]
    # August: hurricane + flood both hit 48157 (1/3 of homes); storm hits 48201 (broad only)
    assert aug[0] == pytest.approx(1 / 3)  # hit share: each county counted once
    assert aug[1] == pytest.approx(2 / 3)  # expected number of disasters: 2 x 1/3
    assert aug[2] == pytest.approx(1.0)  # broad: both counties hit
    assert aug[3] == pytest.approx(4 / 3)  # broad: 1/3 + 1/3 + 2/3
    # September: fire in 48201 (2/3 of homes)
    assert sep[0] == pytest.approx(2 / 3)


def test_exposure_uses_only_past_months(built):
    exposure = {m: r[4] for m, r in climate_rows(built, "770").items()}
    assert exposure[date(2017, 8, 1)] == pytest.approx(0)  # the current month is excluded
    assert exposure[date(2017, 9, 1)] == pytest.approx(2 / 3)  # August
    assert exposure[date(2017, 10, 1)] == pytest.approx(4 / 3)  # August + September
    assert exposure[date(2017, 11, 1)] == pytest.approx(4 / 3)  # window Aug-Oct
    assert exposure[date(2017, 12, 1)] == pytest.approx(2 / 3)  # window Sep-Nov
    assert exposure[date(2018, 1, 1)] == pytest.approx(0)  # window Oct-Dec


def test_unaffected_zip3_has_zero_exposure(built):
    rows = climate_rows(built, "781")
    assert len(rows) == 8  # full monthly grid, June 2017 - January 2018
    assert all(r[0] == 0 and r[4] == 0 for r in rows.values())


def test_post_2010_county_codes_are_recoded(built):
    rows = climate_rows(built, "577")
    assert rows[date(2017, 9, 1)][0] == pytest.approx(1.0)  # FEMA 46102 -> 2010 code 46113


def test_manual_zip3_follows_its_county(built):
    rows = climate_rows(built, "772")  # Houston P.O. boxes -> Harris County
    assert rows[date(2017, 9, 1)][0] == pytest.approx(1.0)


def test_availability_lag_delays_exposure(tmp_path):
    raw, geo = tmp_path / "raw", tmp_path / "geo"
    raw.mkdir()
    (raw / "crosswalk.txt").write_text(CROSSWALK)
    (raw / "fema.csv").write_text(fema_csv())
    census.parse(raw / "crosswalk.txt", geo / "zcta_county.parquet")
    fema.parse(raw / "fema.csv", tmp_path / "declarations.parquet")
    cfg = config()
    cfg["exposure"]["availability_lag_months"] = 1
    out = tmp_path / "processed"
    climate.run(geo, tmp_path / "declarations.parquet", out, tmp_path / "s.md", cfg)
    exposure = {m: r[4] for m, r in climate_rows(out, "770").items()}
    assert exposure[date(2017, 9, 1)] == pytest.approx(0)
    assert exposure[date(2017, 10, 1)] == pytest.approx(2 / 3)
    assert exposure[date(2018, 1, 1)] == pytest.approx(2 / 3)  # September still counts
