"""Tests for the through-the-cycle PD anchoring (Phase 7)."""

import numpy as np
import pandas as pd
import pytest

from climrisk.models.ttc import by_vintage, long_run_rate


def test_long_run_rate_weighs_each_vintage_equally():
    vintage = pd.Series([2007] * 1000 + [2015] * 100)
    y = pd.Series([1] * 100 + [0] * 900 + [0] * 100)  # 10% in 2007, 0% in 2015
    assert long_run_rate(vintage, y) == pytest.approx(0.05)  # not 100 / 1100 = 9.1%


def test_by_vintage_aggregates_rates_and_mean_pds():
    vintage = pd.Series([2007, 2007, 2015, 2015])
    t = by_vintage(
        vintage,
        np.array([1, 0, 0, 0]),
        np.array([0.2, 0.4, 0.1, 0.1]),
        np.array([0.1, 0.3, 0.05, 0.05]),
    )
    assert t["vintage"].tolist() == [2007, 2015]
    assert t["default_rate"].tolist() == [0.5, 0.0]
    assert t["mean_pd_raw"].tolist() == pytest.approx([0.3, 0.1])
    assert t["mean_pd_ttc"].tolist() == pytest.approx([0.2, 0.05])
