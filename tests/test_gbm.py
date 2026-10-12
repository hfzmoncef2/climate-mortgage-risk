"""Tests for the LightGBM challenger PD model (Phase 7)."""

import numpy as np
import pandas as pd
import pytest

from climrisk.models.gbm import GBM
from climrisk.models.metrics import classification_metrics
from climrisk.models.scorecard import Scorecard

PARAMS = {
    "learning_rate": 0.1,
    "num_leaves": 15,
    "min_child_samples": 50,
    "subsample": 0.8,
    "subsample_freq": 1,
    "colsample_bytree": 1.0,
    "reg_lambda": 1.0,
    "n_estimators": 2000,
    "early_stopping_rounds": 30,
    "metric": "auc",
    "seed": 0,
}


def _loans(n: int, seed: int) -> tuple[pd.DataFrame, np.ndarray]:
    """Risk is high when exactly one of (low FICO, high LTV) holds: a pure interaction,
    invisible to an additive model (each feature alone carries no signal)."""
    rng = np.random.default_rng(seed)
    df = pd.DataFrame(
        {
            "fico": rng.integers(600, 820, n).astype(float),
            "ltv": rng.integers(50, 97, n).astype(float),
            "channel": rng.choice(["R", "B", "C"], n),
        }
    )
    risky = (df["fico"] < 710) ^ (df["ltv"] > 73)
    logit = -5 + 2.5 * risky + 0.5 * (df["channel"] == "B")
    y = (rng.uniform(size=n) < 1 / (1 + np.exp(-logit))).astype(int).to_numpy()
    return df, y


def test_gbm_early_stops_and_captures_an_interaction():
    train, y_tr = _loans(40_000, 1)
    valid, y_va = _loans(20_000, 2)
    valid.loc[valid.index[:10], "channel"] = "NEW"  # category never seen in training
    gbm = GBM(["fico", "ltv"], ["channel"], PARAMS).fit(train, y_tr, valid, y_va)
    assert 0 < gbm.n_trees < PARAMS["n_estimators"]
    p = gbm.predict_proba(valid)
    assert np.isfinite(p).all()
    card = Scorecard(["fico", "ltv"], ["channel"]).fit(train, y_tr)
    auc_gbm = classification_metrics(y_va, p)["auc"]
    auc_card = classification_metrics(y_va, card.predict_proba(valid))["auc"]
    assert auc_gbm > auc_card + 0.1  # additive WoE cannot represent the interaction
    imp = gbm.importance()
    assert imp["gain_share"].sum() == pytest.approx(1.0)
    assert set(imp["feature"].head(2)) == {"fico", "ltv"}


def test_gbm_refit_uses_a_fixed_number_of_trees():
    train, y_tr = _loans(10_000, 3)
    gbm = GBM(["fico", "ltv"], ["channel"], PARAMS).fit(train, y_tr, n_estimators=25)
    assert gbm.n_trees == 25
    assert gbm.model.booster_.num_trees() == 25
