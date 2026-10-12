"""Discrimination and calibration metrics for PD models (Phase 7)."""

from __future__ import annotations

import numpy as np
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score, roc_curve


def classification_metrics(y: np.ndarray, p: np.ndarray) -> dict[str, float]:
    """AUC, Gini, KS (discrimination); Brier, log loss, mean PD vs default rate (calibration)."""
    y = np.asarray(y, dtype=int)
    p = np.clip(np.asarray(p, dtype=float), 1e-6, 1 - 1e-6)
    auc = roc_auc_score(y, p)
    fpr, tpr, _ = roc_curve(y, p)
    return {
        "n": int(len(y)),
        "defaults": int(y.sum()),
        "auc": float(auc),
        "gini": float(2 * auc - 1),
        "ks": float(np.max(tpr - fpr)),
        "brier": float(brier_score_loss(y, p)),
        "log_loss": float(log_loss(y, p, labels=[0, 1])),
        "mean_pd_pct": float(100 * p.mean()),
        "default_rate_pct": float(100 * y.mean()),
    }
