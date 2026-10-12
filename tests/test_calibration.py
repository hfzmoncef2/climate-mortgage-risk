"""Tests for the PD calibration diagnostics (Phase 7)."""

import numpy as np
import pytest

from climrisk.models.calibration import (
    apply_shift,
    calibration_table,
    level_shift,
    logit,
    recalibration,
)


def _sample(n: int = 200_000, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    z = rng.normal(-4.5, 1.2, n)  # true logit of default, mean PD about 2%
    p_true = 1 / (1 + np.exp(-z))
    return (rng.uniform(size=n) < p_true).astype(int), p_true


def test_recalibration_recovers_level_and_spread_errors():
    y, p = _sample()
    a, b = recalibration(y, p)
    assert a == pytest.approx(0, abs=0.15) and b == pytest.approx(1, abs=0.05)
    too_high = apply_shift(p, 1.0)  # every PD inflated: level error
    a, b = recalibration(y, too_high)
    assert a == pytest.approx(-1.0, abs=0.1) and b == pytest.approx(1, abs=0.05)
    too_extreme = 1 / (1 + np.exp(-(2 * logit(p) + 4.5)))  # PDs spread too much
    _, b = recalibration(y, too_extreme)
    assert b == pytest.approx(0.5, abs=0.05)


def test_calibration_table_has_equal_count_ordered_bins():
    y, p = _sample(50_000, 1)
    t = calibration_table(y, p, 10)
    assert len(t) == 10 and t["loans"].sum() == 50_000
    assert t["loans"].max() - t["loans"].min() <= 1
    assert t["mean_pd"].is_monotonic_increasing
    assert t["defaults"].sum() == y.sum()
    # a calibrated PD: observed rate tracks the prediction in the riskiest decile
    assert t["default_rate"].iloc[-1] == pytest.approx(t["mean_pd"].iloc[-1], rel=0.1)


def test_level_shift_hits_the_target_and_keeps_the_ranking():
    _, p = _sample(20_000, 2)
    a = level_shift(p, 0.006)
    q = apply_shift(p, a)
    assert q.mean() == pytest.approx(0.006, rel=1e-6)
    assert (np.argsort(q) == np.argsort(p)).all()
