"""Through-the-cycle (TTC) anchoring of the scorecard PD (Phase 7).

The scorecard uses origination data only, with no macroeconomic variable: it can rank
loans but cannot know whether next year is a boom or a crisis. Its PD is therefore
anchored to the development-sample average of vintage default rates:
    1. refit the scorecard on train + validation (2004-2018);
    2. long-run rate = simple average of the yearly (vintage) default rates;
    3. shift every logit(PD) by the same constant so that the mean PD equals that rate
       (ranking and AUC unchanged).
The resulting PD is too high in benign years and too low in crisis years by design: the
gap is the credit cycle, which the stress test (Phase 8) adds back as a scenario.

Outputs:
    reports/pd_ttc.md
    reports/figures/fig11_pd_ttc_by_vintage.png

Usage:
    uv run --frozen python -m climrisk.models.ttc
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from climrisk.causal import event_study as es
from climrisk.features import pd_features
from climrisk.features.target import PROCESSED_DIR
from climrisk.models.calibration import apply_shift, level_shift
from climrisk.models.scorecard import Scorecard, load_features
from climrisk.viz.eda import COLORS, apply_style, md_table

log = logging.getLogger(__name__)


def long_run_rate(vintage: pd.Series, y: pd.Series) -> float:
    """Simple average of the yearly default rates (each vintage weighs the same)."""
    return float(
        pd.Series(np.asarray(y, dtype=float), index=vintage.values).groupby(level=0).mean().mean()
    )


def by_vintage(
    vintage: pd.Series, y: np.ndarray, pd_raw: np.ndarray, pd_ttc: np.ndarray
) -> pd.DataFrame:
    d = pd.DataFrame({"vintage": vintage.to_numpy(), "y": y, "raw": pd_raw, "ttc": pd_ttc})
    return (
        d.groupby("vintage")
        .agg(
            loans=("y", "size"),
            defaults=("y", "sum"),
            default_rate=("y", "mean"),
            mean_pd_raw=("raw", "mean"),
            mean_pd_ttc=("ttc", "mean"),
        )
        .reset_index()
    )


def fig_by_vintage(table: pd.DataFrame, long_run: float, path: Path) -> None:
    fig, ax = es.plt_subplots(figsize=(8, 4.2))
    x = table["vintage"]
    ax.axhline(100 * long_run, color=COLORS["muted"], linewidth=1, linestyle="--", zorder=1)
    ax.annotate(
        f"long-run average {100 * long_run:.2f}%",
        (x.iloc[-1] + 0.3, 100 * long_run),
        xytext=(0, 4),
        textcoords="offset points",
        va="bottom",
        fontsize=8,
        color=COLORS["muted"],
    )
    series = (
        ("default_rate", "Observed default rate", COLORS["focus"]),
        ("mean_pd_ttc", "Mean TTC PD (scorecard)", COLORS["focus_2"]),
    )
    for col, label, color in series:
        ax.plot(
            x,
            100 * table[col],
            "-o",
            color=color,
            linewidth=2,
            markersize=6,
            markeredgecolor=COLORS["surface"],
            markeredgewidth=1.5,
            label=label,
            zorder=3,
        )
    peak = table.loc[table["default_rate"].idxmax()]
    ax.annotate(
        f"{int(peak.vintage)}: {100 * peak.default_rate:.1f}% observed\n"
        f"vs {100 * peak.mean_pd_ttc:.1f}% TTC PD",
        (peak.vintage, 100 * peak.default_rate),
        xytext=(12, -4),
        textcoords="offset points",
        fontsize=8.5,
        color=COLORS["ink_secondary"],
    )
    ax.set_xticks(x, [str(v) for v in x], fontsize=8.5)
    ax.set_xlim(x.min() - 0.5, x.max() + 2.2)
    ax.set_ylim(0, None)
    ax.set_ylabel("24-month default rate (%)")
    ax.set_xlabel("Origination vintage")
    ax.set_title("A through-the-cycle PD: right on average, not year by year", loc="left")
    ax.legend(frameon=False, loc="upper right", fontsize=8.5)
    fig.tight_layout()
    es.save(fig, path)


def run(
    processed_dir: Path = PROCESSED_DIR,
    reports_dir: Path = es.REPORTS_DIR,
    cfg: dict | None = None,
) -> dict:
    cfg = cfg or pd_features.load_config()
    f = cfg["features"]
    apply_style()
    df = load_features(processed_dir)
    fit = df[df["split"].isin(cfg["ttc"]["fit_splits"])]
    y = fit["default_24m"].to_numpy()

    card = Scorecard.from_config(cfg, f["numeric"], f["categorical"]).fit(fit, y)
    p_raw = card.predict_proba(fit)
    target = long_run_rate(fit["vintage"], fit["default_24m"])
    a = level_shift(p_raw, target)
    p_ttc = apply_shift(p_raw, a)
    table = by_vintage(fit["vintage"], y, p_raw, p_ttc)
    log.info(
        "Refit on %s loans; long-run rate %.3f%%, shift a = %.3f", f"{len(fit):,}", 100 * target, a
    )

    reports_dir.mkdir(parents=True, exist_ok=True)
    fig_by_vintage(table, target, reports_dir / "figures" / "fig11_pd_ttc_by_vintage.png")
    rows = [
        (
            int(r.vintage),
            int(r.loans),
            int(r.defaults),
            100 * r.default_rate,
            100 * r.mean_pd_raw,
            100 * r.mean_pd_ttc,
            r.default_rate / r.mean_pd_ttc,
        )
        for r in table.itertuples()
    ]
    sections = [
        "# Through-the-cycle PD anchoring",
        "Generated by `python -m climrisk.models.ttc`. Scorecard refitted on the training and "
        f"validation vintages ({len(fit):,} loans, {len(card.selected)} features kept). "
        "Test vintages not used.",
        "![TTC PD by vintage](figures/fig11_pd_ttc_by_vintage.png)",
        "## Anchoring",
        md_table(
            ["quantity", "value"],
            [
                ("Loan-weighted default rate, 2004-2018 (%)", 100 * y.mean()),
                ("Long-run rate: average of yearly rates (%)", 100 * target),
                ("Mean PD before anchoring (%)", 100 * p_raw.mean()),
                ("Mean PD after anchoring (%)", 100 * p_ttc.mean()),
                ("Logit shift a", a),
            ],
            {1: "{:.3f}"},
        ),
        "## By vintage",
        "observed / TTC PD > 1 means observed defaults exceed the model mean for that vintage. "
        "Residual differences may reflect the credit cycle, omitted variables or model error; "
        "they do not isolate a causal macroeconomic effect. This is a sample-based TTC proxy, "
        "not a regulatory calibration. Phase 8 explores stress scenarios.",
        md_table(
            [
                "vintage",
                "loans",
                "defaults",
                "default_rate_pct",
                "mean_pd_raw_pct",
                "mean_pd_ttc_pct",
                "observed / ttc",
            ],
            rows,
            {3: "{:.2f}", 4: "{:.2f}", 5: "{:.2f}", 6: "{:.2f}"},
        ),
    ]
    (reports_dir / "pd_ttc.md").write_text("\n\n".join(sections) + "\n", encoding="utf-8")
    log.info("Wrote %s", reports_dir / "pd_ttc.md")
    return {"model": card, "shift": a, "long_run": target, "table": table}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()


if __name__ == "__main__":
    main()
