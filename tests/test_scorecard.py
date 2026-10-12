"""Tests for the WoE scorecard and PD metrics (Phase 7)."""

import numpy as np
import pandas as pd
import pytest

from climrisk.models.metrics import classification_metrics, psi, quantile_edges
from climrisk.models.scorecard import MISSING, OTHER, Scorecard, WoEBinner


def test_metrics_on_a_perfect_and_a_random_score():
    y = np.array([0, 0, 0, 1, 1])
    perfect = classification_metrics(y, np.array([0.1, 0.2, 0.3, 0.8, 0.9]))
    assert perfect["auc"] == pytest.approx(1.0) and perfect["ks"] == pytest.approx(1.0)
    flat = classification_metrics(y, np.full(5, 0.4))
    assert flat["auc"] == pytest.approx(0.5) and flat["gini"] == pytest.approx(0.0)
    assert flat["mean_pd_pct"] == pytest.approx(40.0)
    assert flat["default_rate_pct"] == pytest.approx(40.0)


def test_woe_sign_missing_bin_and_unseen_category():
    x = pd.Series([600] * 50 + [800] * 50 + [None] * 10, dtype="float")
    y = np.array([1] * 25 + [0] * 25 + [1] * 2 + [0] * 48 + [1] * 5 + [0] * 5)
    b = WoEBinner("fico", numeric=True, n_bins=2).fit(x, y)
    w = b.transform(pd.Series([600.0, 800.0, None]))
    assert w[1] > 0 > w[0]  # high FICO = safer = positive WoE
    assert w[2] < 0 and MISSING in b.woe  # missing FICO is its own (risky) bin
    assert b.iv > 0.3

    c = pd.Series(["R"] * 60 + ["B"] * 39 + ["T"])
    yc = np.array([0] * 55 + [1] * 5 + [0] * 30 + [1] * 9 + [1])
    cb = WoEBinner("channel", numeric=False, min_share=0.05).fit(c, yc)
    assert "T" not in cb.keep and OTHER in cb.woe  # rare category pooled
    assert cb.transform(pd.Series(["Z"]))[0] == cb.woe[OTHER]  # unseen -> 'other'


def test_scorecard_ranks_risk_and_has_negative_coefficients():
    rng = np.random.default_rng(1)
    n = 20_000
    df = pd.DataFrame(
        {
            "fico": rng.integers(600, 820, n).astype(float),
            "ltv": rng.integers(50, 97, n).astype(float),
            "noise": rng.normal(size=n),
            "occupancy": rng.choice(["P", "I", "S"], n, p=[0.85, 0.1, 0.05]),
        }
    )
    logit = (
        -4 - 0.02 * (df["fico"] - 700) + 0.04 * (df["ltv"] - 80) + 0.8 * (df["occupancy"] == "I")
    )
    y = (rng.uniform(size=n) < 1 / (1 + np.exp(-logit))).astype(int).to_numpy()
    card = Scorecard(["fico", "ltv", "noise"], ["occupancy"]).fit(df, y)
    m = classification_metrics(y, card.predict_proba(df))
    assert m["auc"] > 0.75
    assert m["mean_pd_pct"] == pytest.approx(m["default_rate_pct"], abs=0.2)  # in-sample
    s = card.summary().set_index("feature")
    # pure noise still gets a small in-sample IV (about bins / defaults): far below FICO
    assert s.index[0] == "fico" and s.loc["noise", "iv"] < 0.05 < s.loc["fico", "iv"]
    assert (s.loc[["fico", "ltv", "occupancy"], "coef"] < 0).all()


def test_selection_drops_weak_and_redundant_features():
    rng = np.random.default_rng(2)
    n = 30_000
    fico = rng.integers(600, 820, n).astype(float)
    ltv = rng.integers(50, 97, n).astype(float)
    df = pd.DataFrame(
        {
            "fico": fico,
            "fico_copy": fico + rng.normal(0, 2, n),  # same information as fico
            "ltv": ltv,
            "noise": rng.normal(size=n),
        }
    )
    logit = -4 - 0.02 * (fico - 700) + 0.04 * (ltv - 80)
    y = (rng.uniform(size=n) < 1 / (1 + np.exp(-logit))).astype(int)
    card = Scorecard(["fico", "fico_copy", "ltv", "noise"], [], min_iv=0.05, max_corr=0.8)
    card.fit(df, y)
    assert set(card.selected) == {"fico", "ltv"} or set(card.selected) == {"fico_copy", "ltv"}
    assert card.dropped["noise"].startswith("IV <")
    dup = ({"fico", "fico_copy"} - set(card.selected)).pop()
    assert card.dropped[dup].startswith("WoE corr")
    assert (card.model.coef_[0] < 0).all()
    assert card.summary().set_index("feature").loc["noise", "status"] != "kept"


def test_forced_features_skip_the_iv_filter():
    rng = np.random.default_rng(4)
    n = 20_000
    df = pd.DataFrame({"fico": rng.integers(600, 820, n).astype(float), "weak": rng.normal(size=n)})
    logit = -4 - 0.02 * (df["fico"] - 700) + 0.05 * df["weak"]
    y = (rng.uniform(size=n) < 1 / (1 + np.exp(-logit))).astype(int).to_numpy()
    card = Scorecard(["fico", "weak"], [], min_iv=0.5, force=["weak"]).fit(df, y)
    assert card.binners["weak"].iv < 0.5 and "weak" in card.selected


def test_psi_is_zero_for_the_same_distribution_and_large_for_a_shift():
    rng = np.random.default_rng(5)
    x = rng.normal(size=50_000)
    assert psi(x, rng.normal(size=50_000)) < 0.01
    assert psi(x, rng.normal(1.0, 1.0, 50_000)) > 0.25
    with_missing = np.where(rng.uniform(size=50_000) < 0.3, np.nan, rng.normal(size=50_000))
    assert psi(x, with_missing) > 0.25  # 30% missing vs none is a large shift


def test_point_mass_at_zero_gets_its_own_bin():
    rng = np.random.default_rng(6)
    n = 20_000
    x = np.where(rng.uniform(size=n) < 0.93, 0.0, rng.exponential(1.0, n))
    y = (rng.uniform(size=n) < np.where(x > 0, 0.05, 0.01)).astype(int)
    b = WoEBinner("recent_events", numeric=True).fit(pd.Series(x), y)
    w = b.transform(pd.Series([0.0, 0.5]))
    assert w[0] > 0 > w[1]
    assert b.iv > 0.05
    shifted = np.where(rng.uniform(size=n) < 0.80, 0.0, rng.exponential(1.0, n))
    assert psi(x, shifted) > 0.05
    assert len(quantile_edges(np.arange(1000.0), 10)) == 9
