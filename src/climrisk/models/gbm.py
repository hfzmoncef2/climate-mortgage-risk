"""Challenger PD model: gradient-boosted trees (LightGBM), compared with the scorecard (Phase 7).

Same features and same training vintages as the WoE scorecard. Categorical features use
LightGBM's native handling, with the categories seen in training (unseen ones -> missing).
The number of trees is chosen by early stopping on validation AUC (ranking only: validation
has a different default rate, which would distort log loss). Validation is the tuning
sample, so its scores are slightly optimistic. The honest comparison is on the
test vintages, after refitting on train + validation (end of Phase 7).

Outputs:
    reports/pd_models.md

Usage:
    uv run --frozen python -m climrisk.models.gbm
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from climrisk.causal import event_study as es
from climrisk.features import pd_features
from climrisk.features.target import PROCESSED_DIR
from climrisk.models.metrics import classification_metrics
from climrisk.models.scorecard import Scorecard, load_features
from climrisk.viz.eda import md_table

log = logging.getLogger(__name__)


@dataclass
class GBM:
    numeric: list[str]
    categorical: list[str]
    params: dict
    categories: dict = field(default_factory=dict)
    model: lgb.LGBMClassifier | None = None

    @classmethod
    def from_config(cls, cfg: dict, numeric: list[str], categorical: list[str]) -> GBM:
        return cls(numeric, categorical, dict(cfg["gbm"]))

    @property
    def features(self) -> list[str]:
        return self.numeric + self.categorical

    def _matrix(self, df: pd.DataFrame) -> pd.DataFrame:
        x = df[self.numeric].astype(float).copy()
        for c in self.categorical:
            s = df[c].astype(str)
            s = s.where(s.isin(self.categories[c]))  # unseen category -> missing
            x[c] = pd.Categorical(s, categories=self.categories[c])
        return x[self.features]

    def fit(
        self,
        df: pd.DataFrame,
        y: np.ndarray,
        eval_df: pd.DataFrame | None = None,
        eval_y: np.ndarray | None = None,
        n_estimators: int | None = None,
    ) -> GBM:
        """With an eval sample: early stopping. Without: exactly `n_estimators` trees."""
        p = dict(self.params)
        stop = int(p.pop("early_stopping_rounds"))
        seed = int(p.pop("seed"))
        if n_estimators is not None:
            p["n_estimators"] = int(n_estimators)
        self.categories = {c: sorted(df[c].astype(str).unique()) for c in self.categorical}
        self.model = lgb.LGBMClassifier(**p, random_state=seed, verbose=-1)
        kwargs = {}
        if eval_df is not None:
            ev = self._matrix(eval_df)
            # LightGBM >= 4.7 renamed eval_set to eval_X / eval_y
            if "eval_X" in inspect.signature(lgb.LGBMClassifier.fit).parameters:
                kwargs = {"eval_X": (ev,), "eval_y": (eval_y,)}
            else:
                kwargs = {"eval_set": [(ev, eval_y)]}
            kwargs["callbacks"] = [lgb.early_stopping(stop, verbose=False)]
        self.model.fit(self._matrix(df), y, **kwargs)
        return self

    @property
    def n_trees(self) -> int:
        best = self.model.best_iteration_
        return int(best) if best else int(self.model.n_estimators)

    def predict_proba(self, df: pd.DataFrame) -> np.ndarray:
        return self.model.predict_proba(self._matrix(df), num_iteration=self.n_trees)[:, 1]

    def importance(self) -> pd.DataFrame:
        gain = self.model.booster_.feature_importance(importance_type="gain")
        return (
            pd.DataFrame({"feature": self.features, "gain_share": gain / gain.sum()})
            .sort_values("gain_share", ascending=False)
            .reset_index(drop=True)
        )


def run(
    processed_dir: Path = PROCESSED_DIR,
    reports_dir: Path = es.REPORTS_DIR,
    cfg: dict | None = None,
) -> dict:
    cfg = cfg or pd_features.load_config()
    f = cfg["features"]
    df = load_features(processed_dir)
    train, valid = df[df["split"] == "train"], df[df["split"] == "validation"]
    y_tr, y_va = train["default_24m"].to_numpy(), valid["default_24m"].to_numpy()

    card = Scorecard.from_config(cfg, f["numeric"], f["categorical"]).fit(train, y_tr)
    gbm = GBM.from_config(cfg, f["numeric"], f["categorical"]).fit(train, y_tr, valid, y_va)
    log.info("LightGBM: %d trees (early stopping)", gbm.n_trees)

    rows = []
    for name, model in (("WoE scorecard", card), ("LightGBM", gbm)):
        for split, d, y in (("train", train, y_tr), ("validation", valid, y_va)):
            m = classification_metrics(y, model.predict_proba(d))
            rows.append(
                (
                    name,
                    split,
                    m["auc"],
                    m["gini"],
                    m["ks"],
                    m["brier"],
                    m["log_loss"],
                    m["mean_pd_pct"],
                    m["default_rate_pct"],
                )
            )
            log.info("%s %s: AUC %.4f", name, split, m["auc"])

    sections = [
        "# PD models: scorecard vs LightGBM (no climate features)",
        "Generated by `python -m climrisk.models.gbm`. "
        "Both models start from the same feature pool; "
        "the scorecard selects features on train. They "
        "are fitted on the training vintages. LightGBM's number of trees "
        f"({gbm.n_trees}) is chosen by early stopping on the validation vintages, so its "
        "validation scores are slightly optimistic; the test vintages are not used here.",
        "## Performance",
        md_table(
            [
                "model",
                "sample",
                "auc",
                "gini",
                "ks",
                "brier",
                "log_loss",
                "mean_pd_pct",
                "default_rate_pct",
            ],
            rows,
            {i: "{:.4f}" for i in range(2, 9)},
        ),
        "## LightGBM feature importance (share of total gain)",
        md_table(
            ["feature", "gain_share"], list(gbm.importance().itertuples(index=False)), {1: "{:.3f}"}
        ),
    ]
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "pd_models.md").write_text("\n\n".join(sections) + "\n", encoding="utf-8")
    log.info("Wrote %s", reports_dir / "pd_models.md")
    return {"rows": rows, "scorecard": card, "gbm": gbm}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()


if __name__ == "__main__":
    main()
