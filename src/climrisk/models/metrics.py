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


def quantile_edges(values: np.ndarray, n_bins: int) -> np.ndarray:
    """Training quantile edges; isolate a minimum holding at least 1/n_bins of values."""
    v = np.asarray(values, dtype=float)
    v = v[~np.isnan(v)]
    if not len(v):
        return np.array([])
    edges = np.quantile(v, np.linspace(0, 1, n_bins + 1)[1:-1])
    lowest = v.min()
    if np.mean(v == lowest) >= 1 / n_bins:
        edges = np.append(edges, np.nextafter(lowest, np.inf))
    return np.unique(edges[edges > lowest])


def psi(expected: np.ndarray, actual: np.ndarray, n_bins: int = 10) -> float:
    """Population stability index of a feature between two samples (e.g. train -> later).

    Bins use quantile_edges on `expected`, plus one bin for missing values. Rule of thumb:
    < 0.1 stable, 0.1-0.25 moderate shift, > 0.25 large shift.
    """
    e = np.asarray(expected, dtype=float)
    a = np.asarray(actual, dtype=float)
    edges = quantile_edges(e, n_bins)

    def shares(x: np.ndarray) -> np.ndarray:
        idx = np.where(np.isnan(x), len(edges) + 1, np.searchsorted(edges, x, side="right"))
        counts = np.bincount(idx, minlength=len(edges) + 2).astype(float)
        return np.clip(counts / counts.sum(), 1e-6, None)

    pe, pa = shares(e), shares(a)
    return float(np.sum((pa - pe) * np.log(pa / pe)))
