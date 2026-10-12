"""Tests for the final PD evaluation helpers (Phase 7)."""

import numpy as np
from sklearn.metrics import roc_auc_score

from climrisk.models.final import bootstrap_aucs, interval


def test_bootstrap_aucs_are_paired_and_bracket_the_estimate():
    rng = np.random.default_rng(0)
    n = 20_000
    x1, x2 = rng.normal(size=n), rng.normal(size=n)
    y = (rng.uniform(size=n) < 1 / (1 + np.exp(-(-3 + x1 + 0.8 * x2)))).astype(int)
    preds = {"weak": x1, "strong": x1 + 0.8 * x2}
    draws = bootstrap_aucs(y, preds, n_boot=200, seed=1)
    assert list(draws.columns) == ["weak", "strong"] and len(draws) == 200
    for name, p in preds.items():
        lo, hi = interval(draws[name])
        assert lo < roc_auc_score(y, p) < hi
    lo, hi = interval(draws["strong"] - draws["weak"])
    assert lo > 0  # the stronger score wins on every paired resample interval
    single_lo, single_hi = interval(draws["strong"])
    assert hi - lo < single_hi - single_lo  # pairing narrows the interval of a difference
