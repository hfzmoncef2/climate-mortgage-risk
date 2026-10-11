"""Guard against accidental publication of local data."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_data_is_ignored():
    result = subprocess.run(
        ["git", "check-ignore", "--stdin"],
        input="data/raw/loans.csv\ndata/interim/panel.parquet\ndata/processed/train.csv\n",
        text=True,
        capture_output=True,
        cwd=ROOT,
        check=True,
    )
    assert len(result.stdout.splitlines()) == 3


def test_no_data_is_tracked():
    result = subprocess.run(
        ["git", "ls-files", "--", "data/"],
        text=True,
        capture_output=True,
        cwd=ROOT,
        check=True,
    )
    assert not result.stdout.strip(), "Data files are tracked by Git."
