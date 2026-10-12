"""Tests for the credit-default sensitivity of the PD model (D022)."""

import duckdb
import numpy as np
import pandas as pd
import pytest

from climrisk.models.scorecard import Scorecard
from climrisk.models.sensitivity import load_eligible, ttc_predictions
from climrisk.models.ttc import long_run_rate


def _write(df: pd.DataFrame, path) -> None:
    con = duckdb.connect()
    con.register("df", df)
    con.execute(f"COPY (SELECT * FROM df) TO '{path}' (FORMAT PARQUET)")


def test_load_eligible_keeps_eligible_loans_with_both_targets(tmp_path):
    _write(
        pd.DataFrame({"loan_id": ["A", "B", "C"], "split": "test", "default_24m": [1, 1, 0]}),
        tmp_path / "pd_features.parquet",
    )
    _write(
        pd.DataFrame(
            {
                "loan_id": ["A", "B", "C"],
                "credit_eligible": [True, False, True],
                "credit_default_24m": [0, 1, 0],
            }
        ),
        tmp_path / "credit_default.parquet",
    )
    df = load_eligible(tmp_path).set_index("loan_id")
    assert list(df.index) == ["A", "C"]
    assert df.loc["A", "default_24m"] == 1 and df.loc["A", "credit_default_24m"] == 0


def test_ttc_predictions_are_anchored_to_the_long_run_rate_of_the_target():
    rng = np.random.default_rng(0)
    n = 30_000
    fit = pd.DataFrame(
        {"fico": rng.integers(600, 820, n).astype(float), "vintage": rng.choice([2007, 2015], n)}
    )
    logit = -4 - 0.02 * (fit["fico"] - 700) + 1.5 * (fit["vintage"] == 2007)
    fit["y"] = (rng.uniform(size=n) < 1 / (1 + np.exp(-logit))).astype(int)
    card = Scorecard(["fico"], []).fit(fit, fit["y"].to_numpy())
    p_fit = ttc_predictions(card, fit, fit, "y")
    assert p_fit.mean() == pytest.approx(long_run_rate(fit["vintage"], fit["y"]), rel=1e-6)
