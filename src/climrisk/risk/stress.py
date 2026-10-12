"""Expected-loss stress test of the recent book: recession vs hurricane (Phase 8).

Book: the PD-sample loans of the test vintages (2021-2024). For each loan, over 24 months:
    EL = PD x LGD x EAD,  EAD = original balance (no amortisation: slightly conservative)
PD is the final primary scorecard (D021) refitted on 2004-2018 and through-the-cycle
anchored (D020). LGD comes from the measured defaults (risk/lgd.py), as a range: low /
central / high (unresolved defaults counted as no loss / left out / a loss exit).

Scenarios:
    baseline            TTC PD, benign LGD (defaults of 2017-2021)
    recession           PD x m, downturn LGD (defaults of 2005-2012); m = observed default
                        rate / mean TTC PD of the 2007-2008 vintages (in-sample)
    hurricane           baseline + extra PD on coastal loans (Phase 5 hurricane effect x
                        coastal share), at the LGD of disaster-flagged defaults
    hurricane in a      recession + the hurricane's extra PD at the high LGD of
    recession           disaster-flagged defaults
    recent conditions   baseline with PD x the observed / TTC ratio of the test vintages
Break-even: how many times larger the hurricane's default effect would need to be for its
loss to match the recession's increment.
These are illustrative, transparent scenarios built from the project's estimates, not
forecasts; each assumption is in configs/stress.yaml.

Outputs:
    reports/stress_test.md
    reports/figures/fig13_stress_expected_loss.png

Usage:
    uv run --frozen python -m climrisk.risk.stress
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from climrisk.causal import event_study as es
from climrisk.features import pd_features
from climrisk.features.target import PROCESSED_DIR
from climrisk.models.final import PRIMARY, fit_final_models
from climrisk.models.sensitivity import ttc_predictions
from climrisk.risk.lgd import load_config
from climrisk.viz.eda import COLORS, apply_style, md_table

LEVELS = ("low", "central", "high")

log = logging.getLogger(__name__)


def lgd_levels(defaults: pd.DataFrame, mask: pd.Series) -> dict[str, float]:
    """LGD of a group of defaults: low / central / high (see risk/lgd.py)."""
    d = defaults[mask]
    res = d[d["lgd_resolved"]]
    return {
        "low": float(d["loss_low"].sum() / d["ead"].sum()),
        "central": float(res["loss"].sum() / res["ead"].sum()),
        "high": float(d["loss_high"].sum() / d["ead"].sum()),
    }


def downturn_multiplier(
    y: np.ndarray, p_ttc: np.ndarray, vintage: np.ndarray, vintages: list[int]
) -> float:
    """Observed default rate / mean TTC PD, pooled over the downturn vintages."""
    m = np.isin(vintage, vintages)
    return float(y[m].mean() / p_ttc[m].mean())


def expected_loss(pd_: np.ndarray, lgd: float, ead: np.ndarray) -> float:
    return float(np.sum(np.clip(pd_, 0, 1) * lgd * ead))


def scenario_table(
    book: pd.DataFrame, lgd: dict, m: float, extra_pd: np.ndarray, recent_factor: float = 1.0
) -> pd.DataFrame:
    """EL (% of book balance) of each scenario at each LGD level."""
    ead, p = book["ead"].to_numpy(), book["pd_ttc"].to_numpy()
    p_down = np.minimum(p * m, 1)
    total = ead.sum()
    rows = []
    for lvl in LEVELS:
        base = expected_loss(p, lgd["benign"][lvl], ead)
        down = expected_loss(p_down, lgd["downturn"][lvl], ead)
        hurricane = expected_loss(extra_pd, lgd["disaster"][lvl], ead)
        hurricane_stressed = expected_loss(extra_pd, lgd["disaster"]["high"], ead)
        rows += [
            ("Baseline", lvl, base),
            ("Hurricane", lvl, base + hurricane),
            ("Recession (2007-08 type)", lvl, down),
            ("Hurricane in a recession", lvl, down + hurricane_stressed),
            ("Hurricane alone (increment)", lvl, hurricane),
            (
                "Baseline, recent-conditions PD",
                lvl,
                expected_loss(p * recent_factor, lgd["benign"][lvl], ead),
            ),
        ]
    t = pd.DataFrame(rows, columns=["scenario", "level", "el"])
    t["el_pct"] = 100 * t["el"] / total
    return t.pivot(index="scenario", columns="level", values="el_pct")[list(LEVELS)]


def break_even(table: pd.DataFrame) -> dict[str, float]:
    """How many times larger the hurricane's default effect would have to be for its expected
    loss to match the recession's increment over the baseline (central recession), at the
    central and at the high disaster LGD."""
    gap = table.loc["Recession (2007-08 type)", "central"] - table.loc["Baseline", "central"]
    inc = table.loc["Hurricane alone (increment)"]
    return {
        lvl: float(gap / inc[lvl]) if inc[lvl] > 0 else float("inf") for lvl in ("central", "high")
    }


def fig_stress(table: pd.DataFrame, path: Path) -> None:
    order = ["Baseline", "Hurricane", "Recession (2007-08 type)", "Hurricane in a recession"]
    t = table.loc[order]
    fig, ax = es.plt_subplots(figsize=(8, 3.4))
    y = np.arange(len(order))[::-1]
    ax.barh(y, t["central"], height=0.55, color=COLORS["focus"], zorder=2)
    ax.hlines(y, t["low"], t["high"], color=COLORS["ink_secondary"], linewidth=1.5, zorder=3)
    for yi, (lo, mid, hi) in zip(y, t[["low", "central", "high"]].to_numpy(), strict=True):
        ax.text(
            max(hi, mid) + 0.01 * t["high"].max(),
            yi,
            f"{mid:.3f}%  ({lo:.3f}-{hi:.3f})",
            va="center",
            fontsize=8.5,
            color=COLORS["ink"],
        )
    ax.set_yticks(y, order)
    ax.set_xlim(0, t["high"].max() * 1.45)
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", visible=True)
    ax.set_xlabel("24-month expected loss (% of book balance)")
    fig.suptitle(
        "Expected loss of the 2021-2024 book: a hurricane vs a recession",
        x=0.01,
        ha="left",
        fontweight="bold",
        fontsize=11.5,
    )
    fig.text(
        0.01,
        -0.03,
        "Bar: central LGD; line: LGD range (unresolved defaults counted as "
        "no loss / as a loss exit). Illustrative scenarios, see reports/stress_test.md.",
        fontsize=8,
        color=COLORS["muted"],
    )
    fig.tight_layout()
    es.save(fig, path)


def run(
    processed_dir: Path = PROCESSED_DIR,
    reports_dir: Path = es.REPORTS_DIR,
    cfg: dict | None = None,
    pd_cfg: dict | None = None,
) -> dict:
    cfg = cfg or load_config()
    pd_cfg = pd_cfg or pd_features.load_config()
    s, fin = cfg["scenarios"], pd_cfg["final"]
    apply_style()

    # PD: the final primary model, through-the-cycle anchored
    feature_path = str(processed_dir / "pd_features.parquet").replace("'", "''")
    df = duckdb.sql(f"SELECT * FROM read_parquet('{feature_path}')").df()
    upb = duckdb.sql(f"SELECT loan_id, orig_upb FROM '{processed_dir / 'loans.parquet'}'").df()
    df = df.merge(upb, on="loan_id", how="left", validate="one_to_one")
    fit = df[df["split"].isin(fin["fit_splits"])]
    train, valid = df[df["split"] == "train"], df[df["split"] == "validation"]
    model = fit_final_models(pd_cfg, fit, train, valid)[PRIMARY]
    book = df[df["split"] == s["book_split"]].copy()
    book["pd_ttc"] = ttc_predictions(model, fit, book, "default_24m")
    book["ead"] = book["orig_upb"].astype(float)
    p_fit = ttc_predictions(model, fit, fit, "default_24m")
    m = downturn_multiplier(
        fit["default_24m"].to_numpy(), p_fit, fit["vintage"].to_numpy(), s["downturn_vintages"]
    )

    # LGD ranges measured on the defaults
    defaults = duckdb.sql(f"SELECT * FROM '{processed_dir / 'lgd_defaults.parquet'}'").df()
    lgd = {
        name: lgd_levels(defaults, defaults["default_period"].isin(periods))
        for name, periods in s["lgd_periods"].items()
    }
    lgd["disaster"] = lgd_levels(defaults, defaults["default_type"] == "disaster-flagged")

    # Hurricane: extra default probability on coastal loans
    effect = float(s["hurricane"]["delinquency_effect_pp"]) / 100
    extra_pd = effect * book["near_coast_share"].fillna(0).to_numpy()

    recent = float(s.get("recent_conditions_pd_factor", 1.0))
    table = scenario_table(book, lgd, m, extra_pd, recent)
    k = break_even(table)
    log.info("Downturn multiplier %.2f; EL (%% of balance):\n%s", m, table.round(4))
    fig_stress(table, reports_dir / "figures" / "fig13_stress_expected_loss.png")

    balance = book["ead"].sum()
    lgd_rows = [(name, *(100 * v[lvl] for lvl in LEVELS)) for name, v in lgd.items()]
    el_rows = [
        (name, *row, balance * row[1] / 100 / 1e6)
        for name, row in zip(table.index, table.to_numpy(), strict=True)
    ]
    sections = [
        "# Stress test: expected loss of the 2021-2024 book",
        "Generated by `python -m climrisk.risk.stress`. Book: "
        f"{len(book):,} PD-sample loans originated 2021-2024, balance "
        f"${balance / 1e9:.2f} bn (EAD = original balance). PD = final primary scorecard "
        f"(D021), through-the-cycle anchored (mean TTC PD {100 * book['pd_ttc'].mean():.2f}%). "
        f"24-month horizon. Illustrative scenarios built from the project's estimates; every "
        "assumption is in `configs/stress.yaml`.",
        "![Stress test](figures/fig13_stress_expected_loss.png)",
        "## Inputs",
        f"Recession PD multiplier m = {m:.2f} (observed default / mean TTC PD of the "
        f"{', '.join(map(str, s['downturn_vintages']))} vintages, in-sample). Hurricane: "
        f"+{100 * effect:.3f} pp default probability x coastal share; mean extra PD over the "
        f"book {100 * extra_pd.mean():.3f}% ({int((extra_pd > 0).sum()):,} exposed loans).",
        "### LGD used (% of EAD)",
        md_table(
            ["defaults", "low", "central", "high"],
            lgd_rows,
            {1: "{:.2f}", 2: "{:.2f}", 3: "{:.2f}"},
        ),
        "## Expected loss by scenario (% of book balance)",
        "The TTC PD under-predicted the test vintages (D023): the recent-conditions row "
        "scales the baseline "
        f"PD by {recent:.2f}, the observed / TTC ratio of the D004 primary model on the "
        "2021-2024 test vintages. The hurricane increment does not "
        "depend on the PD level; a "
        "higher PD level would raise the recession loss further.",
        "`central_usd_m`: central expected loss in million dollars for this book. The "
        "hurricane in a recession uses the high disaster LGD (forbearance may cure less when "
        "house prices fall).",
        md_table(
            ["scenario", "low", "central", "high", "central_usd_m"],
            el_rows,
            {1: "{:.4f}", 2: "{:.4f}", 3: "{:.4f}", 4: "{:.2f}"},
        ),
    ]
    sections += [
        "## How robust is the hurricane conclusion?",
        "For the hurricane's expected loss to match the recession's increment over the "
        f"baseline, its default effect would have to be {k['central']:,.0f} times larger "
        f"at the central disaster LGD, or {k['high']:,.0f} times larger at the high "
        "disaster LGD, on top of the already severe assumptions of the scenario. "
        "These are linear loss-equivalent multiples, not feasible probability forecasts: "
        "large multiples would exceed a 100% default probability and are not re-simulated "
        "with probability caps.",
    ]
    (reports_dir / "stress_test.md").write_text("\n\n".join(sections) + "\n", encoding="utf-8")
    log.info("Wrote %s", reports_dir / "stress_test.md")
    return {"table": table, "multiplier": m, "lgd": lgd, "break_even": k}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()


if __name__ == "__main__":
    main()
