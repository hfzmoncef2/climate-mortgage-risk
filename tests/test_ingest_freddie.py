"""Ingestion tests using synthetic data only."""

from datetime import date

import duckdb
import pytest

from climrisk.ingest.freddie import ingest_year, load_schema

YEAR = 2005


def orig_row(loan_id: str, fico: str, dti: str, postal: str) -> str:
    fields = [
        fico,
        "200503",
        "N",
        "203502",
        "17140",
        "0",
        "1",
        "P",
        "80",
        dti,
        "200000",
        "80",
        "5.875",
        "R",
        "N",
        "FRM",
        "OH",
        "SF",
        postal,
        loan_id,
        "P",
        "360",
        "2",
        "OTHER",
        "N",
        "",
        "",
        "N",
        "7",
        "N",
        "9999",
    ]
    assert len(fields) == 31
    return "|".join(fields)


def perf_row(loan_id: str, period: str, status: str, zero_balance_code: str = "") -> str:
    fields = [""] * 35
    fields[0], fields[1], fields[2], fields[3] = loan_id, period, "150000.00", status
    fields[4], fields[5], fields[8], fields[10] = "12", "348", zero_balance_code, "5.875"
    return "|".join(fields)


@pytest.fixture
def ingested(tmp_path):
    raw, out = tmp_path / "raw", tmp_path / "out"
    raw.mkdir()
    (raw / f"sample_orig_{YEAR}.txt").write_text(
        orig_row("F05Q1TEST001", "750", "36", "432")
        + "\n"
        + orig_row("F05Q1TEST002", "9999", "999", "000")
        + "\n"
    )
    (raw / f"sample_perf_{YEAR}.txt").write_text(
        perf_row("F05Q1TEST001", "200504", "00")
        + "\n"
        + perf_row("F05Q1TEST001", "200505", "XX")
        + "\n"
        + perf_row("F05Q1TEST002", "200504", "RA")
        + "\n"
        + perf_row("F05Q1TEST002", "200505", "00", zero_balance_code="01")
        + "\n"
    )
    with duckdb.connect() as con:
        counts = ingest_year(con, load_schema(), YEAR, raw_dir=raw, out_dir=out)
        yield con, out, counts


def read(con, path, columns):
    return con.execute(
        f"SELECT {columns} FROM read_parquet(?) ORDER BY 1, 2", [str(path)]
    ).fetchall()


def test_row_counts(ingested):
    _, _, counts = ingested
    assert counts == {"origination": 2, "performance": 4}


def test_missing_codes_become_null(ingested):
    con, out, _ = ingested
    rows = read(
        con,
        out / "origination" / f"{YEAR}.parquet",
        "loan_id, fico, dti, postal_code, valuation_method, vantage_score",
    )
    assert rows == [
        ("F05Q1TEST001", 750, 36, "432", None, None),
        ("F05Q1TEST002", None, None, None, None, None),
    ]


def test_types_and_dates(ingested):
    con, out, _ = ingested
    rows = read(
        con,
        out / "origination" / f"{YEAR}.parquet",
        "loan_id, first_payment_date, orig_rate, vintage, orig_quarter, source_year",
    )
    assert rows[0] == ("F05Q1TEST001", date(2005, 3, 1), 5.875, 2005, 1, YEAR)


def test_performance_codes_stay_strings(ingested):
    con, out, _ = ingested
    rows = read(
        con,
        out / "performance" / f"{YEAR}.parquet",
        "loan_id, period, delinquency_status, zero_balance_code",
    )
    assert rows == [
        ("F05Q1TEST001", date(2005, 4, 1), "00", None),
        ("F05Q1TEST001", date(2005, 5, 1), None, None),
        ("F05Q1TEST002", date(2005, 4, 1), "RA", None),
        ("F05Q1TEST002", date(2005, 5, 1), "00", "01"),
    ]


def test_malformed_value_fails_loudly(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / f"sample_orig_{YEAR}.txt").write_text(
        orig_row("F05Q1TEST003", "abc", "36", "432") + "\n"
    )
    (raw / f"sample_perf_{YEAR}.txt").write_text(perf_row("F05Q1TEST003", "200504", "00") + "\n")
    with duckdb.connect() as con, pytest.raises(duckdb.Error):
        ingest_year(con, load_schema(), YEAR, raw_dir=raw, out_dir=tmp_path / "out")

    assert not list((tmp_path / "out").rglob("*.parquet"))
    assert not list((tmp_path / "out").rglob("*.tmp"))


def test_vintage_comes_from_loan_id_not_file(tmp_path):
    """D003: a 2009 Q1 loan appears in the 2008 source file."""
    raw = tmp_path / "raw"
    raw.mkdir()
    fields = orig_row("F09Q1TEST001", "750", "36", "432").split("|")
    fields[1], fields[3] = "200903", "203902"
    (raw / "sample_orig_2008.txt").write_text("|".join(fields) + "\n")
    (raw / "sample_perf_2008.txt").write_text(perf_row("F09Q1TEST001", "200904", "00") + "\n")
    with duckdb.connect() as con:
        ingest_year(con, load_schema(), 2008, raw_dir=raw, out_dir=tmp_path / "out")
        for table in ("origination", "performance"):
            path = tmp_path / "out" / table / "2008.parquet"
            row = con.execute(
                "SELECT vintage, orig_quarter, source_year FROM read_parquet(?)", [str(path)]
            ).fetchone()
            assert row == (2009, 1, 2008)
