"""Tests for the default target and the ZIP3 x month panel, on synthetic loans.

All synthetic loans have their first payment in 2005-03 (see orig_row), and the tests
use a 3-month window: 2005-03, 2005-04, 2005-05.
"""

from datetime import date

import duckdb
import pytest
from test_ingest_freddie import orig_row, perf_row

from climrisk.features import panel, target
from climrisk.ingest.freddie import ingest_year, load_schema

YEAR = 2005

# loan suffix -> list of (period, delinquency status, zero balance code[, zero balance date])
HISTORIES = {
    "001": [("200503", "01", ""), ("200504", "02", ""), ("200505", "03", "")],  # 90+ dpd
    "002": [("200503", "01", ""), ("200504", "RA", "")],  # REO acquisition
    "003": [("200503", "01", ""), ("200504", "01", ""), ("200505", "02", "09")],  # loss exit
    "004": [("200503", "00", ""), ("200504", "00", "01")],  # prepaid
    "005": [("200503", "00", ""), ("200504", "00", "16")],  # transferred out
    "006": [("200503", "00", ""), ("200504", "00", ""), ("200505", "00", "")],  # performing
    "007": [
        ("200503", "00", ""),
        ("200504", "00", ""),
        ("200505", "00", ""),
        ("200506", "03", ""),
    ],  # defaults only after the window
    "008": [("200503", "00", "")],  # missing months
    "009": [
        ("200502", "05", ""),
        ("200503", "00", ""),
        ("200504", "00", ""),
        ("200505", "00", ""),
    ],  # bad status before first payment: ignored
    "010": [("200503", "00", ""), ("200505", "00", "")],  # April missing: gap inside window
    "011": [
        ("200503", "00", ""),
        ("200504", "00", ""),
        ("200506", "00", "01", "200504"),
    ],  # paid off in April, exit row reported in June
    "012": [
        ("200503", "00", ""),
        ("200504", "00", ""),
        ("200505", "00", ""),
        ("200507", "00", "01"),
    ],  # paid off after the window
}

HISTORIES.update(
    {
        "013": [("200503", "00", ""), ("200507", "00", "")],
        "014": [("200505", "00", ""), ("200506", "00", "")],
    }
)

EXPECTED = {
    "001": ("default", 1),
    "002": ("default", 1),
    "003": ("default", 1),
    "004": ("prepaid", 0),
    "005": ("censored_transfer", None),
    "006": ("performing", 0),
    "007": ("performing", 0),
    "008": ("censored_gap", None),
    "009": ("performing", 0),
    "010": ("performing", 0),
    "011": ("prepaid", 0),
    "012": ("performing", 0),
}

EXPECTED.update({"013": ("performing", 0), "014": ("performing", 0)})


def perf_line(loan_id, period, status, zbc, zb_date=""):
    fields = perf_row(loan_id, period, status, zbc).split("|")
    fields[9] = zb_date  # Zero Balance Effective Date
    return "|".join(fields)


def config(cutoff: str = "2026-03-01") -> dict:
    cfg = target.load_config()
    cfg["horizon_months"] = 3
    cfg["performance_cutoff"] = cutoff
    return cfg


@pytest.fixture
def interim(tmp_path):
    raw, out = tmp_path / "raw", tmp_path / "interim"
    raw.mkdir()
    ids = {s: f"F05Q1TEST{s}" for s in HISTORIES}
    (raw / f"sample_orig_{YEAR}.txt").write_text(
        "\n".join(orig_row(ids[s], "750", "36", "432") for s in HISTORIES) + "\n"
    )
    perf_lines = [perf_line(ids[s], *row) for s, rows in HISTORIES.items() for row in rows]
    (raw / f"sample_perf_{YEAR}.txt").write_text("\n".join(perf_lines) + "\n")
    with duckdb.connect() as con:
        ingest_year(con, load_schema(), YEAR, raw_dir=raw, out_dir=out)
    return out


def outcomes(path) -> dict:
    rows = duckdb.sql(
        f"SELECT loan_id, outcome, default_24m FROM read_parquet('{path}')"
    ).fetchall()
    return {loan_id[-3:]: (outcome, label) for loan_id, outcome, label in rows}


def test_labels(interim, tmp_path):
    path = target.run(interim, tmp_path / "processed", tmp_path / "summary.md", config())
    assert outcomes(path) == EXPECTED


def test_window_beyond_cutoff_is_censored(interim, tmp_path):
    path = target.run(interim, tmp_path / "processed", tmp_path / "s.md", config("2005-04-01"))
    result = outcomes(path)
    assert result["006"] == ("censored_cutoff", None)
    # Incomplete window -> censored even for a loan that defaults: otherwise recent
    # vintages would keep their early defaults but lose their performing loans.
    assert result["001"] == ("censored_cutoff", None)
    assert all(value == ("censored_cutoff", None) for value in result.values())


def test_pd_sample_is_labelled_loans_only(interim, tmp_path):
    path = target.run(interim, tmp_path / "processed", tmp_path / "s.md", config())
    labelled, in_sample = duckdb.sql(
        f"SELECT count(default_24m), count(*) FILTER (WHERE in_pd_sample) "
        f"FROM read_parquet('{path}')"
    ).fetchone()
    assert (labelled, in_sample) == (12, 11)


def test_panel_counts(interim, tmp_path):
    path = panel.run(interim, tmp_path / "processed", config())
    rows = duckdb.sql(
        f"SELECT period, n_loans, n_dlq30, n_dlq60, n_dlq90, n_prepaid, n_loss_exit "
        f"FROM read_parquet('{path}') WHERE zip3 = '432' ORDER BY period"
    ).fetchall()
    by_period = {r[0]: r[1:] for r in rows}
    assert date(2005, 2, 1) not in by_period  # months before first payment are dropped
    # 2005-05: 001 (03), 003 (02 + loss exit), 006, 007, 009, 010, 012 (00)
    assert by_period[date(2005, 5, 1)] == (8, 2, 2, 1, 0, 1)
    # 2005-04: 001 (02), 002 (RA), 003 (01), 004 (prepaid), 005, 006, 007, 009, 011, 012
    assert by_period[date(2005, 4, 1)] == (10, 3, 2, 1, 1, 0)


def build(tmp_path, orig_lines, histories):
    raw, out = tmp_path / "raw", tmp_path / "interim"
    raw.mkdir()
    (raw / f"sample_orig_{YEAR}.txt").write_text("\n".join(orig_lines) + "\n")
    (raw / f"sample_perf_{YEAR}.txt").write_text("\n".join(histories) + "\n")
    with duckdb.connect() as con:
        ingest_year(con, load_schema(), YEAR, raw_dir=raw, out_dir=out)
    return out


@pytest.mark.parametrize(
    "exit_code, expected", [("01", ("prepaid", 0)), ("16", ("censored_transfer", None))]
)
def test_exit_before_later_default_takes_precedence(tmp_path, exit_code, expected):
    interim = build(
        tmp_path,
        [orig_row("F05Q1TEST001", "750", "36", "432")],
        [
            perf_row("F05Q1TEST001", "200503", "00", exit_code),
            perf_row("F05Q1TEST001", "200504", "03"),
        ],
    )
    path = target.run(interim, tmp_path / "processed", tmp_path / "s.md", config())
    assert outcomes(path)["001"] == expected


@pytest.mark.parametrize("kind", ["harp", "pre_harp", "date", "covid", "zip"])
def test_pd_exclusions_do_not_remove_loans_or_labels(tmp_path, kind):
    fields = orig_row("F05Q1TEST001", "750", "36", "432").split("|")
    periods = ["200503", "200504", "200505"]
    if kind == "harp":
        fields[27] = "Y"
    elif kind == "pre_harp":
        fields[25] = "F01Q1PREHARP"
    elif kind == "zip":
        fields[18] = "000"
    else:
        fields[1] = "200703" if kind == "date" else "202003"
        periods = (
            ["200703", "200704", "200705"] if kind == "date" else ["202003", "202004", "202005"]
        )
    interim = build(
        tmp_path, ["|".join(fields)], [perf_row("F05Q1TEST001", p, "00") for p in periods]
    )
    path = target.run(interim, tmp_path / "processed", tmp_path / "s.md", config())
    assert outcomes(path)["001"] == ("performing", 0)
    with duckdb.connect() as con:
        assert con.execute("SELECT in_pd_sample FROM read_parquet(?)", [str(path)]).fetchone() == (
            False,
        )
    if kind == "zip":
        panel_path = panel.run(interim, tmp_path / "processed", config())
        with duckdb.connect() as con:
            assert con.execute(
                "SELECT count(*) FROM read_parquet(?)", [str(panel_path)]
            ).fetchone() == (0,)


@pytest.mark.parametrize(
    "code, expected",
    [("01", ("prepaid", 0)), ("09", ("default", 1)), ("16", ("censored_transfer", None))],
)
def test_late_report_uses_effective_exit_date(tmp_path, code, expected):
    interim = build(
        tmp_path,
        [orig_row("F05Q1TEST001", "750", "36", "432")],
        [
            perf_line("F05Q1TEST001", "200503", "00", ""),
            perf_line("F05Q1TEST001", "200506", "00", code, "200504"),
        ],
    )
    path = target.run(interim, tmp_path / "processed", tmp_path / "s.md", config())
    assert outcomes(path)["001"] == expected
    if code == "09":
        with duckdb.connect() as con:
            assert con.execute(
                "SELECT default_date FROM read_parquet(?)", [str(path)]
            ).fetchone() == (date(2005, 4, 1),)


def test_observations_after_cutoff_do_not_resolve_gap(tmp_path):
    interim = build(
        tmp_path,
        [orig_row("F05Q1TEST001", "750", "36", "432")],
        [
            perf_line("F05Q1TEST001", "200503", "00", ""),
            perf_line("F05Q1TEST001", "200506", "00", "01", "200504"),
        ],
    )
    path = target.run(interim, tmp_path / "processed", tmp_path / "s.md", config("2005-05-01"))
    assert outcomes(path)["001"] == ("censored_gap", None)


def test_latest_exit_code_is_selected_by_reporting_month(tmp_path):
    interim = build(
        tmp_path,
        [orig_row("F05Q1TEST001", "750", "36", "432")],
        [
            perf_line("F05Q1TEST001", "200504", "00", "16", "200504"),
            perf_line("F05Q1TEST001", "200506", "00", "01", "200504"),
        ],
    )
    path = target.run(interim, tmp_path / "processed", tmp_path / "s.md", config())
    assert outcomes(path)["001"] == ("prepaid", 0)


def test_internal_gap_is_counted_and_allowed(tmp_path):
    interim = build(
        tmp_path,
        [orig_row("F05Q1TEST001", "750", "36", "432")],
        [
            perf_line("F05Q1TEST001", "200503", "00", ""),
            perf_line("F05Q1TEST001", "200506", "00", ""),
        ],
    )
    path = target.run(interim, tmp_path / "processed", tmp_path / "s.md", config())
    assert outcomes(path)["001"] == ("performing", 0)
    with duckdb.connect() as con:
        assert con.execute(
            "SELECT window_months_missing FROM read_parquet(?)", [str(path)]
        ).fetchone() == (2,)


def test_gap_measures(interim, tmp_path):
    path = target.run(interim, tmp_path / "processed", tmp_path / "s.md", config())
    rows = duckdb.sql(
        f"SELECT loan_id, start_gap_months, max_internal_gap_months, long_internal_gap "
        f"FROM read_parquet('{path}')"
    ).fetchall()
    gaps = {loan_id[-3:]: rest for loan_id, *rest in rows}
    assert gaps["006"] == [0, 0, False]  # complete history
    assert gaps["010"] == [0, 1, False]  # April missing: tolerated
    assert gaps["013"] == [0, 3, True]  # April-June missing: excluded from the PD sample
    assert gaps["014"] == [2, 0, False]  # seasoned acquisition: tolerated


@pytest.mark.parametrize("status, label, eligible", [("00", 0, False), ("03", 1, True)])
def test_long_gap_excludes_only_non_default_labels(tmp_path, status, label, eligible):
    interim = build(
        tmp_path,
        [orig_row("F05Q1TEST001", "750", "36", "432")],
        [
            perf_line("F05Q1TEST001", "200503", status, ""),
            perf_line("F05Q1TEST001", "200507", "00", ""),
        ],
    )
    path = target.run(interim, tmp_path / "processed", tmp_path / "s.md", config())
    with duckdb.connect() as con:
        row = con.execute(
            "SELECT default_24m, long_internal_gap, in_pd_sample FROM read_parquet(?)",
            [str(path)],
        ).fetchone()
    assert row == (label, True, eligible)
