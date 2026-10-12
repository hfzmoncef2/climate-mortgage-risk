"""Tests for the expected-loss stress test (Phase 8)."""

import numpy as np
import pandas as pd
import pytest

from climrisk.risk.stress import downturn_multiplier, lgd_levels, scenario_table


def test_lgd_levels_low_central_high():
    d = pd.DataFrame(
        {
            "ead": [100.0, 100.0, 200.0],
            "loss": [50.0, 0.0, np.nan],  # third default unresolved
            "loss_low": [50.0, 0.0, 0.0],
            "loss_high": [50.0, 0.0, 100.0],
            "lgd_resolved": [True, True, False],
        }
    )
    lv = lgd_levels(d, pd.Series(True, index=d.index))
    assert lv == pytest.approx({"low": 50 / 400, "central": 50 / 200, "high": 150 / 400})


def test_downturn_multiplier_pools_the_downturn_vintages():
    y = np.array([1, 0, 0, 0, 1, 0, 0, 0, 0, 0])
    p = np.full(10, 0.1)
    vintage = np.array([2007] * 4 + [2008] * 4 + [2015] * 2)
    assert downturn_multiplier(y, p, vintage, [2007, 2008]) == pytest.approx((2 / 8) / 0.1)


def test_scenario_table_arithmetic():
    book = pd.DataFrame({"ead": [100.0, 300.0], "pd_ttc": [0.01, 0.02]})
    lvl = {"low": 0.1, "central": 0.2, "high": 0.4}
    lgd = {"benign": lvl, "downturn": {k: 2 * v for k, v in lvl.items()}, "disaster": lvl}
    extra = np.array([0.0, 0.01])
    t = scenario_table(book, lgd, m=3.0, extra_pd=extra)
    base = (0.01 * 100 + 0.02 * 300) * 0.2  # central
    down = (0.03 * 100 + 0.06 * 300) * 0.4
    hurricane = 0.01 * 300 * 0.2
    stressed_hurricane = 0.01 * 300 * 0.4  # high disaster LGD in a recession
    assert t.loc["Baseline", "central"] == pytest.approx(100 * base / 400)
    assert t.loc["Recession (2007-08 type)", "central"] == pytest.approx(100 * down / 400)
    assert t.loc["Hurricane", "central"] == pytest.approx(100 * (base + hurricane) / 400)
    assert t.loc["Hurricane in a recession", "central"] == pytest.approx(
        100 * (down + stressed_hurricane) / 400
    )
    assert (t["low"] <= t["central"]).all() and (t["central"] <= t["high"]).all()
