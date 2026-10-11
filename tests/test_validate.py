"""Quality checks using synthetic data."""

import duckdb
from test_ingest_freddie import orig_row, perf_row

from climrisk.ingest.freddie import ingest_year, load_schema
from climrisk.ingest.validate import run

YEAR = 2005


def build(tmp_path, orig_lines, perf_lines):
    raw, out = tmp_path / "raw", tmp_path / "out"
    raw.mkdir()
    (raw / f"sample_orig_{YEAR}.txt").write_text("\n".join(orig_lines) + "\n")
    (raw / f"sample_perf_{YEAR}.txt").write_text("\n".join(perf_lines) + "\n")
    with duckdb.connect() as con:
        ingest_year(con, load_schema(), YEAR, raw_dir=raw, out_dir=out)
    return out


def test_clean_data_passes_and_writes_report(tmp_path):
    out = build(
        tmp_path,
        [orig_row("F05Q1TEST001", "750", "36", "432")],
        [perf_row("F05Q1TEST001", "200504", "00"), perf_row("F05Q1TEST001", "200505", "01")],
    )
    report = tmp_path / "data_quality.md"
    assert run(data_dir=out, report_path=report)
    text = report.read_text()
    assert "## Checks" in text
    assert "FAIL (BLOCKING)" not in text


def test_orphan_performance_loan_is_blocking(tmp_path):
    out = build(
        tmp_path,
        [orig_row("F05Q1TEST001", "750", "36", "432")],
        [perf_row("F05Q1TEST999", "200504", "00")],
    )
    assert not run(data_dir=out, report_path=tmp_path / "r.md")


def test_duplicate_loan_month_is_blocking(tmp_path):
    out = build(
        tmp_path,
        [orig_row("F05Q1TEST001", "750", "36", "432")],
        [perf_row("F05Q1TEST001", "200504", "00"), perf_row("F05Q1TEST001", "200504", "00")],
    )
    assert not run(data_dir=out, report_path=tmp_path / "r.md")


def test_out_of_range_value_is_only_a_warning(tmp_path):
    out = build(
        tmp_path,
        [orig_row("F05Q1TEST001", "750", "80", "432")],
        [perf_row("F05Q1TEST001", "200504", "00")],
    )
    report = tmp_path / "r.md"
    assert run(data_dir=out, report_path=report)
    assert "FAIL (warning)" in report.read_text()


def test_source_year_mismatch_is_only_a_warning(tmp_path):
    """Identifier-derived vintage remains valid despite a source-year mismatch."""
    fields = orig_row("F09Q1TEST001", "750", "36", "432").split("|")
    fields[1], fields[3] = "200903", "203902"
    out = build(tmp_path, ["|".join(fields)], [perf_row("F09Q1TEST001", "200904", "00")])
    report = tmp_path / "r.md"
    assert run(data_dir=out, report_path=report)
    text = report.read_text()
    assert "| Identifier vintage equals source file year (D003) | 1 | FAIL (warning) |" in text
    assert "| First payment year is zero or one year after vintage | 0 | OK |" in text
