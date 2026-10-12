"""Calibration diagnosis of the PD models on the validation vintages (Phase 7).

Discrimination (AUC) says whether risky loans are ranked above safe ones; calibration says
whether a predicted 2% means 2% observed. Two diagnostics:
    reliability table   loans sorted into equal-count bins of predicted PD; mean predicted
                        PD vs observed default rate per bin
    level shift a       the constant added to logit(PD) so that the mean PD equals the
                        observed default rate (calibration-in-the-large; 0 if right on
                        average, < 0 if PDs are too high)
    slope b             from logit P(default) = c + b * logit(PD), unpenalised: 1 if the
                        PDs are neither too extreme (b < 1) nor too flat (b > 1)
A level error with b close to 1 is fixed by shifting the intercept only (next step).

Outputs:
    reports/pd_calibration.md
    reports/figures/fig10_pd_calibration.png

Usage:
    uv run --frozen python -m climrisk.models.calibration
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import brentq
from sklearn.linear_model import LogisticRegression

from climrisk.causal import event_study as es
from climrisk.features import pd_features
from climrisk.features.target import PROCESSED_DIR
from climrisk.models.gbm import GBM
from climrisk.models.scorecard import Scorecard, load_features
from climrisk.viz.eda import COLORS, apply_style, md_table

EPS = 1e-6

log = logging.getLogger(__name__)


def logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(np.asarray(p, dtype=float), EPS, 1 - EPS)
    return np.log(p / (1 - p))


def calibration_table(y: np.ndarray, p: np.ndarray, n_bins: int = 10) -> pd.DataFrame:
    """Equal-count bins of predicted PD (bin 1 = safest)."""
    d = pd.DataFrame({"y": np.asarray(y, dtype=int), "p": np.asarray(p, dtype=float)})
    d["bin"] = pd.qcut(d["p"].rank(method="first"), n_bins, labels=False) + 1
    out = d.groupby("bin").agg(
        loans=("y", "size"),
        defaults=("y", "sum"),
        mean_pd=("p", "mean"),
        default_rate=("y", "mean"),
    )
    return out.reset_index()


def apply_shift(p: np.ndarray, a: float) -> np.ndarray:
    """PD with its logit shifted by a (ranking unchanged)."""
    return 1 / (1 + np.exp(-(logit(p) + a)))


def level_shift(p: np.ndarray, target_rate: float) -> float:
    """The a such that mean(apply_shift(p, a)) = target_rate."""
    return float(brentq(lambda a: apply_shift(p, a).mean() - target_rate, -20, 20))


def recalibration(y: np.ndarray, p: np.ndarray) -> tuple[float, float]:
    """(level shift a, slope b): see the module docstring."""
    y = np.asarray(y, dtype=int)
    model = LogisticRegression(C=1e12, max_iter=1000)  # effectively unpenalised
    model.fit(logit(p).reshape(-1, 1), y)
    return level_shift(p, y.mean()), float(model.coef_[0, 0])


def fig_calibration(tables: dict[str, pd.DataFrame], path: Path) -> None:
    from matplotlib.ticker import FuncFormatter

    fig, ax = es.plt_subplots(figsize=(6.2, 5.0))
    colors = {"WoE scorecard": COLORS["focus"], "LightGBM": COLORS["focus_2"]}
    values = pd.concat([100 * t[c] for t in tables.values() for c in ("mean_pd", "default_rate")])
    values = values[values > 0]
    lo, hi = values.min() * 0.7, values.max() * 1.4
    ax.plot([lo, hi], [lo, hi], color=COLORS["muted"], linewidth=1, linestyle="--", zorder=1)
    ax.text(
        0.97,
        0.04,
        "dashed line: perfect calibration",
        transform=ax.transAxes,
        ha="right",
        va="bottom",
        fontsize=8,
        color=COLORS["muted"],
    )
    for name, t in tables.items():
        rate = (100 * t["default_rate"]).where(t["default_rate"] > 0)  # log axis: skip zeros
        ax.plot(
            100 * t["mean_pd"],
            rate,
            "-o",
            color=colors[name],
            linewidth=2,
            markersize=6,
            markeredgecolor=COLORS["surface"],
            markeredgewidth=1.5,
            label=name,
            zorder=3,
        )
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlim(lo, hi)
    ax.set_ylim(lo, hi)
    plain = FuncFormatter(lambda v, _: f"{v:g}")
    ax.xaxis.set_major_formatter(plain)
    ax.yaxis.set_major_formatter(plain)
    ax.grid(axis="both", which="major", visible=True)
    ax.set_xlabel("Mean predicted PD per decile (%, log scale)")
    ax.set_ylabel("Observed 24-month default rate (%, log scale)")
    ax.set_title("PD calibration on the validation vintages (2015-2018)", loc="left")
    ax.legend(frameon=False, loc="upper left", fontsize=8.5)
    fig.tight_layout()
    es.save(fig, path)


def run(
    processed_dir: Path = PROCESSED_DIR,
    reports_dir: Path = es.REPORTS_DIR,
    cfg: dict | None = None,
) -> dict:
    cfg = cfg or pd_features.load_config()
    f = cfg["features"]
    n_bins = int(cfg["calibration"]["bins"])
    apply_style()
    df = load_features(processed_dir)
    train, valid = df[df["split"] == "train"], df[df["split"] == "validation"]
    y_tr, y_va = train["default_24m"].to_numpy(), valid["default_24m"].to_numpy()

    models = {
        "WoE scorecard": Scorecard.from_config(cfg, f["numeric"], f["categorical"]).fit(
            train, y_tr
        ),
        "LightGBM": GBM.from_config(cfg, f["numeric"], f["categorical"]).fit(
            train, y_tr, valid, y_va
        ),
    }
    tables, fits = {}, {}
    for name, model in models.items():
        p = model.predict_proba(valid)
        tables[name] = calibration_table(y_va, p, n_bins)
        fits[name] = recalibration(y_va, p)
        log.info("%s: a = %.3f, b = %.3f", name, *fits[name])

    reports_dir.mkdir(parents=True, exist_ok=True)
    fig_calibration(tables, reports_dir / "figures" / "fig10_pd_calibration.png")
    sections = [
        "# PD calibration on the validation vintages",
        "Generated by `python -m climrisk.models.calibration`. Models fitted on the training "
        "vintages (2004-2014); calibration measured on the validation vintages (2015-2018).",
        "![Calibration](figures/fig10_pd_calibration.png)",
        "## Level and slope",
        "Perfect calibration: observed / predicted = 1, a = 0, b = 1. a is the shift of "
        "logit(PD) that makes the mean PD equal the observed rate (a < 0: PDs too high). "
        "b < 1: PDs too extreme; b > 1: PDs too flat.",
        md_table(
            [
                "model",
                "mean_pd_pct",
                "default_rate_pct",
                "observed / predicted",
                "a (level)",
                "b (slope)",
            ],
            [
                (
                    n,
                    100 * (t["mean_pd"] * t["loans"]).sum() / t["loans"].sum(),
                    100 * t["defaults"].sum() / t["loans"].sum(),
                    t["defaults"].sum() / (t["mean_pd"] * t["loans"]).sum(),
                    *fits[n],
                )
                for n, t in tables.items()
            ],
            {1: "{:.3f}", 2: "{:.3f}", 3: "{:.2f}", 4: "{:.3f}", 5: "{:.3f}"},
        ),
    ]
    for name, t in tables.items():
        rows = [
            (
                int(r.bin),
                int(r.loans),
                int(r.defaults),
                100 * r.mean_pd,
                100 * r.default_rate,
                r.default_rate / r.mean_pd,
            )
            for r in t.itertuples()
        ]
        sections += [
            f"## Reliability table: {name}",
            md_table(
                [
                    "decile",
                    "loans",
                    "defaults",
                    "mean_pd_pct",
                    "default_rate_pct",
                    "observed / predicted",
                ],
                rows,
                {3: "{:.3f}", 4: "{:.3f}", 5: "{:.2f}"},
            ),
        ]
    (reports_dir / "pd_calibration.md").write_text("\n\n".join(sections) + "\n", encoding="utf-8")
    log.info("Wrote %s", reports_dir / "pd_calibration.md")
    return {"tables": tables, "fits": fits}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()


if __name__ == "__main__":
    main()
