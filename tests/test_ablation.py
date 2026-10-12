"""Tests for the climate ablation of the PD model (Phase 7)."""

import numpy as np

from climrisk.models.ablation import paired_auc_gain


def _data(n: int = 30_000, seed: int = 0):
    rng = np.random.default_rng(seed)
    x1, x2 = rng.normal(size=n), rng.normal(size=n)
    y = (rng.uniform(size=n) < 1 / (1 + np.exp(-(-3 + x1 + 0.8 * x2)))).astype(int)
    return y, x1, x1 + 0.8 * x2


def test_no_gain_when_the_predictions_are_identical():
    y, base, _ = _data()
    gain, lo, hi = paired_auc_gain(y, base, base.copy(), n_boot=50, seed=1)
    assert gain == 0 and lo == 0 and hi == 0


def test_gain_detected_when_the_new_feature_is_informative():
    y, base, new = _data()
    gain, lo, hi = paired_auc_gain(y, base, new, n_boot=100, seed=1)
    assert gain > 0.03 and 0 < lo < gain < hi


def test_paired_interval_is_narrow_for_a_useless_feature():
    y, base, _ = _data()
    noisy = base + np.random.default_rng(3).normal(0, 0.01, len(base))
    gain, lo, hi = paired_auc_gain(y, base, noisy, n_boot=100, seed=1)
    assert lo <= 0 <= hi or abs(gain) < 1e-3
    assert hi - lo < 0.005  # pairing removes the sampling noise common to both models
