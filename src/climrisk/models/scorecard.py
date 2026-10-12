"""Baseline PD model: weight-of-evidence (WoE) scorecard with logistic regression (Phase 7).

The banking standard. Each feature is cut into bins fitted on the training sample:
    numeric      quantile bins, with a separate bin for a point mass at the minimum,
                 plus a 'missing' bin
    categorical  one bin per category; rare ones (< min_bin_share) pooled into 'other'
Each bin is replaced by its weight of evidence, WoE = ln(%goods / %bads): positive = safer
than average. A logistic regression on the WoE columns gives the PD. Information value
(IV) = sum((%goods - %bads) * WoE) ranks the features.

Feature selection (training data only, standard scorecard practice):
    1. drop features with IV < min_iv (no stable signal);
    2. among WoE columns correlated above max_corr, keep the one with the highest IV;
       Features in `force` bypass the IV and correlation filters, but not sign checks.
    3. refit, dropping the lowest-IV feature with a positive (wrong-sign) coefficient,
       until every coefficient is negative (higher WoE = safer = lower PD).

Fitted on train, evaluated on validation (the test sample is untouched until the end).

Outputs:
    reports/pd_scorecard.md

Usage:
    uv run --frozen python -m climrisk.models.scorecard
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

from climrisk.causal import event_study as es
from climrisk.features import pd_features
from climrisk.features.target import PROCESSED_DIR
from climrisk.models.metrics import classification_metrics, quantile_edges
from climrisk.viz.eda import md_table

MISSING, OTHER = "missing", "other"

log = logging.getLogger(__name__)


@dataclass
class WoEBinner:
    """Bins and WoE values for one feature, fitted on training data."""

    name: str
    numeric: bool
    n_bins: int = 10
    min_share: float = 0.005
    smoothing: float = 0.5
    edges: np.ndarray | None = None
    keep: set = field(default_factory=set)
    woe: dict = field(default_factory=dict)
    iv: float = 0.0
    table: pd.DataFrame | None = None

    def bins(self, x: pd.Series) -> pd.Series:
        if self.numeric:
            v = pd.to_numeric(x, errors="coerce").to_numpy(dtype=float)
            idx = np.searchsorted(self.edges, v, side="right")
            labels = np.array([f"b{i:02d}" for i in range(len(self.edges) + 1)], dtype=object)
            out = labels[idx]
            out[np.isnan(v)] = MISSING
            return pd.Series(out, index=x.index)
        s = x.astype(str)
        return s.where(s.isin(self.keep), OTHER)

    def fit(self, x: pd.Series, y: np.ndarray) -> WoEBinner:
        if self.numeric:
            v = pd.to_numeric(x, errors="coerce").to_numpy(dtype=float)
            self.edges = quantile_edges(v, self.n_bins)
        else:
            share = x.astype(str).value_counts(normalize=True)
            self.keep = set(share[share >= self.min_share].index)
        b = self.bins(x)
        counts = pd.crosstab(b, pd.Series(y, index=x.index)).reindex(columns=[0, 1], fill_value=0)
        good = counts[0] + self.smoothing
        bad = counts[1] + self.smoothing
        pg, pb = good / good.sum(), bad / bad.sum()
        woe = np.log(pg / pb)
        self.woe = woe.to_dict()
        self.iv = float(((pg - pb) * woe).sum())
        self.table = pd.DataFrame({"loans": counts.sum(axis=1), "defaults": counts[1], "woe": woe})
        if self.numeric:
            lo = np.concatenate([[-np.inf], self.edges])
            hi = np.concatenate([self.edges, [np.inf]])
            labels = [f"[{a:g}, {c:g})" for a, c in zip(lo, hi, strict=True)]
            v = pd.to_numeric(x, errors="coerce").dropna()
            if len(self.edges) and len(v) and self.edges[0] == np.nextafter(v.min(), np.inf):
                labels[0] = f"= {v.min():g}"
                labels[1] = f"({v.min():g}, {hi[1]:g})"
            ranges = {f"b{i:02d}": label for i, label in enumerate(labels)}
            self.table.index = [ranges.get(i, i) for i in self.table.index]
        return self

    def transform(self, x: pd.Series) -> np.ndarray:
        """Unseen bins (e.g. a category absent from training) get WoE 0 = average risk."""
        return self.bins(x).map(self.woe).fillna(0.0).to_numpy(dtype=float)


@dataclass
class Scorecard:
    numeric: list[str]
    categorical: list[str]
    n_bins: int = 10
    min_share: float = 0.005
    smoothing: float = 0.5
    C: float = 1.0
    min_iv: float = 0.0
    max_corr: float = 1.0
    force: list = field(default_factory=list)  # Skip IV/correlation filters, not sign checks
    binners: dict = field(default_factory=dict)
    selected: list = field(default_factory=list)
    dropped: dict = field(default_factory=dict)
    model: LogisticRegression | None = None

    @classmethod
    def from_config(cls, cfg: dict, numeric: list[str], categorical: list[str]) -> Scorecard:
        s = cfg["scorecard"]
        return cls(
            numeric,
            categorical,
            int(s["numeric_bins"]),
            float(s["min_bin_share"]),
            float(s["woe_smoothing"]),
            float(s["C"]),
            float(s.get("min_iv", 0.0)),
            float(s.get("max_corr", 1.0)),
        )

    @property
    def features(self) -> list[str]:
        return self.numeric + self.categorical

    def _woe_matrix(self, df: pd.DataFrame, cols: list[str]) -> np.ndarray:
        return np.column_stack([self.binners[f].transform(df[f]) for f in cols])

    def fit(self, df: pd.DataFrame, y: np.ndarray) -> Scorecard:
        for f in self.features:
            self.binners[f] = WoEBinner(
                f, f in self.numeric, self.n_bins, self.min_share, self.smoothing
            ).fit(df[f], y)
        by_iv = sorted(self.features, key=lambda f: self.binners[f].iv, reverse=True)
        self.dropped = {}
        candidates = []
        for f in by_iv:
            if self.binners[f].iv < self.min_iv and f not in self.force:
                self.dropped[f] = f"IV < {self.min_iv:g}"
            else:
                candidates.append(f)
        corr = (
            np.atleast_2d(np.corrcoef(self._woe_matrix(df, candidates), rowvar=False))
            if candidates
            else np.empty((0, 0))
        )
        keep: list[str] = []
        for i, f in enumerate(candidates):
            clash = [g for g in keep if abs(corr[i, candidates.index(g)]) > self.max_corr]
            if clash and f not in self.force:
                self.dropped[f] = f"WoE corr > {self.max_corr:g} with {clash[0]}"
            else:
                keep.append(f)
        while True:
            if not keep:
                raise ValueError("No features remain after scorecard selection")
            self.model = LogisticRegression(C=self.C, max_iter=2000)
            self.model.fit(self._woe_matrix(df, keep), y)
            wrong = [f for f, c in zip(keep, self.model.coef_[0], strict=True) if c >= 0]
            if not wrong:
                break
            worst = min(wrong, key=lambda f: self.binners[f].iv)
            self.dropped[worst] = "positive coefficient"
            keep.remove(worst)
        self.selected = keep
        return self

    def predict_proba(self, df: pd.DataFrame) -> np.ndarray:
        return self.model.predict_proba(self._woe_matrix(df, self.selected))[:, 1]

    def summary(self) -> pd.DataFrame:
        coef = dict(zip(self.selected, self.model.coef_[0], strict=True))
        return (
            pd.DataFrame(
                {
                    "feature": self.features,
                    "iv": [self.binners[f].iv for f in self.features],
                    "coef": [coef.get(f, np.nan) for f in self.features],
                    "status": [self.dropped.get(f, "kept") for f in self.features],
                }
            )
            .sort_values("iv", ascending=False)
            .reset_index(drop=True)
        )


def load_features(processed_dir: Path = PROCESSED_DIR) -> pd.DataFrame:
    path = str(processed_dir / "pd_features.parquet").replace("'", "''")
    return duckdb.sql(
        f"SELECT * FROM read_parquet('{path}') WHERE split IN ('train', 'validation')"
    ).df()


def run(
    processed_dir: Path = PROCESSED_DIR,
    reports_dir: Path = es.REPORTS_DIR,
    cfg: dict | None = None,
) -> dict:
    cfg = cfg or pd_features.load_config()
    f = cfg["features"]
    df = load_features(processed_dir)
    train, valid = df[df["split"] == "train"], df[df["split"] == "validation"]
    log.info("Train %s loans, validation %s loans", f"{len(train):,}", f"{len(valid):,}")

    card = Scorecard.from_config(cfg, f["numeric"], f["categorical"])
    card.fit(train, train["default_24m"].to_numpy())
    metrics = {
        "train": classification_metrics(train["default_24m"], card.predict_proba(train)),
        "validation": classification_metrics(valid["default_24m"], card.predict_proba(valid)),
    }
    log.info("Kept %d features; dropped: %s", len(card.selected), card.dropped)
    for split, m in metrics.items():
        log.info("%s: AUC %.3f, Gini %.3f, KS %.3f", split, m["auc"], m["gini"], m["ks"])

    keys = [
        "n",
        "defaults",
        "auc",
        "gini",
        "ks",
        "brier",
        "log_loss",
        "mean_pd_pct",
        "default_rate_pct",
    ]
    fmt = {i: "{:.4f}" for i in range(3, 10)}
    top = card.summary()
    sections = [
        "# PD model: WoE scorecard (baseline, no climate features)",
        "Generated by `python -m climrisk.models.scorecard`. Logistic regression on weight-of-"
        "evidence bins fitted on the training vintages; evaluated on the validation vintages "
        "(the test vintages are not used here). Target: 24-month default (D004).",
        "## Performance",
        md_table(["sample"] + keys, [(s, *[m[k] for k in keys]) for s, m in metrics.items()], fmt),
        "## Features ranked by information value (train)",
        "IV < 0.02 useless, 0.02-0.1 weak, 0.1-0.3 medium, > 0.3 strong. Selection on train "
        f"only: IV >= {card.min_iv:g}, WoE correlation <= {card.max_corr:g}, negative "
        f"coefficients. {len(card.selected)} of {len(card.features)} features kept.",
        md_table(
            ["feature", "iv", "coef", "status"],
            [
                (f, iv, None if np.isnan(c) else c, st)
                for f, iv, c, st in top.itertuples(index=False)
            ],
            {1: "{:.3f}", 2: "{:.3f}"},
        ),
        "## Bins of the strongest feature",
        md_table(
            ["bin", "loans", "defaults", "woe"],
            list(card.binners[top.loc[0, "feature"]].table.itertuples()),
            {3: "{:.3f}"},
        ),
    ]
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "pd_scorecard.md").write_text("\n\n".join(sections) + "\n", encoding="utf-8")
    log.info("Wrote %s", reports_dir / "pd_scorecard.md")
    return {"metrics": metrics, "summary": top, "model": card}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()


if __name__ == "__main__":
    main()
