"""Exploratory figures and tables (Phase 4).

Reads the processed tables and writes:
    reports/figures/fig1_default_by_vintage.png
    reports/figures/fig2_disasters_by_year.png
    reports/figures/fig3_harvey_delinquency.png
    reports/figures/fig4_default_by_fico_ltv.png
    reports/figures/fig5_default_by_exposure.png
    reports/eda.md   (every figure's numbers as a table, so no value is chart-only)

These are raw descriptive patterns. Causal statements come from the event study
(Phase 5), not from these figures.

Usage:
    uv run --frozen python -m climrisk.viz.eda
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import duckdb
import matplotlib

matplotlib.use("Agg")  # file output only, no display needed
import matplotlib.pyplot as plt  # noqa: E402
import yaml  # noqa: E402

from climrisk.features.climate import FEMA_PATH, events_query
from climrisk.features.climate import load_config as load_climate_config
from climrisk.features.target import PROCESSED_DIR  # noqa: E402
from climrisk.ingest.freddie import ROOT  # noqa: E402

CONFIG_PATH = ROOT / "configs" / "eda.yaml"
FIGURES_DIR = ROOT / "reports" / "figures"
REPORT_PATH = ROOT / "reports" / "eda.md"

# Reference palette (light mode): one categorical slot for the focus series, muted ink
# for comparison series, hairline solid grid. One y-axis per panel.
COLORS = {
    "surface": "#fcfcfb",
    "ink": "#0b0b0b",
    "ink_secondary": "#52514e",
    "muted": "#898781",
    "grid": "#e1e0d9",
    "axis": "#c3c2b7",
    "focus": "#2a78d6",  # categorical slot 1 (blue)
    "focus_2": "#eb6834",  # categorical slot 2 (orange)
}

log = logging.getLogger(__name__)


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def apply_style() -> None:
    plt.rcParams.update(
        {
            "figure.facecolor": COLORS["surface"],
            "axes.facecolor": COLORS["surface"],
            "savefig.facecolor": COLORS["surface"],
            "font.family": "sans-serif",
            "font.size": 10,
            "text.color": COLORS["ink"],
            "axes.labelcolor": COLORS["ink_secondary"],
            "axes.titlesize": 12,
            "axes.titleweight": "bold",
            "axes.titlelocation": "left",
            "axes.titlepad": 12,
            "axes.edgecolor": COLORS["axis"],
            "axes.linewidth": 0.8,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "axes.grid.axis": "y",
            "grid.color": COLORS["grid"],
            "grid.linewidth": 0.6,
            "grid.linestyle": "-",
            "xtick.color": COLORS["muted"],
            "ytick.color": COLORS["muted"],
            "xtick.labelcolor": COLORS["ink_secondary"],
            "ytick.labelcolor": COLORS["ink_secondary"],
            "legend.frameon": False,
            "lines.linewidth": 1.8,
        }
    )


def bin_case(column: str, edges: list[float], integer: bool = True) -> str:
    """SQL CASE that labels `column` into ordered bins, e.g. edges [620, 680] ->
    '1: <620', '2: 620-679', '3: >=680' (the number prefix keeps the order).
    With integer=False the upper bound is printed as is: '2: 0.5-1.5'."""
    parts = [f"WHEN {column} < {edges[0]} THEN '1: <{edges[0]:g}'"]
    for i, (lo, hi) in enumerate(zip(edges[:-1], edges[1:], strict=True), start=2):
        upper = hi - 1 if integer else hi
        parts.append(f"WHEN {column} < {hi} THEN '{i}: {lo:g}-{upper:g}'")
    parts.append(f"ELSE '{len(edges) + 1}: >={edges[-1]:g}'")
    return f"CASE WHEN {column} IS NULL THEN NULL {' '.join(parts)} END"


# --- Tables (pure SQL, testable) ------------------------------------------------------


def default_by_vintage(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    return con.execute("""
        SELECT vintage,
               count(default_24m) AS labelled,
               sum(default_24m) AS defaults,
               100.0 * avg(default_24m) AS default_rate_pct,
               avg(overlaps_covid::INT) > 0.5 AS covid_window,  -- most loans excluded
               count(*) FILTER (WHERE in_pd_sample) AS pd_loans,
               100.0 * avg(default_24m) FILTER (WHERE in_pd_sample) AS pd_default_rate_pct
        FROM loans GROUP BY vintage HAVING count(default_24m) > 0 ORDER BY vintage
    """).fetchall()


def disasters_by_year(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    """Share of US homes in counties hit by at least one qualifying disaster per year."""
    return con.execute("""
        WITH zip3_housing AS (
            SELECT zip3, sum(coalesce(housing, 0)) AS housing FROM weights GROUP BY zip3
        ),
        zip3_year AS (
            SELECT w.zip3, e.year, sum(w.weight) AS hit
            FROM weights w
            JOIN (SELECT DISTINCT county_fips, year(month) AS year FROM events) e
              USING (county_fips)
            GROUP BY w.zip3, e.year
        )
        SELECT year, 100.0 * sum(coalesce(hit, 0) * housing) / sum(housing) AS homes_hit_pct
        FROM (SELECT DISTINCT year(month) AS year FROM climate) years
        CROSS JOIN zip3_housing
        LEFT JOIN zip3_year USING (zip3, year)
        WHERE year BETWEEN 2000 AND 2025
        GROUP BY year ORDER BY year
    """).fetchall()


def harvey_series(con: duckdb.DuckDBPyConnection, cfg: dict) -> list[tuple]:
    """Monthly 60+ dpd and disaster-flagged delinquency rates, Houston vs Texas control."""
    h = cfg["harvey"]
    event = date.fromisoformat(h["event_month"])
    texas = " OR ".join(f"zip3 LIKE '{p}%'" for p in h["texas_zip3_prefixes"])
    clean = int(h["control_clean_months"])
    return con.execute(f"""
        WITH treated AS (
            SELECT zip3 FROM climate
            WHERE month = DATE '{event}' AND hit_share_main >= {h["treated_min_hit_share"]}
              AND ({texas})
        ),
        control AS (
            SELECT zip3 FROM climate
            WHERE ({texas})
              AND month BETWEEN DATE '{event}' - INTERVAL {clean} MONTH
                            AND DATE '{event}' + INTERVAL {clean} MONTH
            GROUP BY zip3 HAVING max(hit_share_broad) = 0
        ),
        groups AS (
            SELECT zip3, 'Houston area (hit by Harvey)' AS grp FROM treated
            UNION ALL
            SELECT zip3, 'Rest of Texas (not hit)' FROM control
        )
        SELECT grp,
               date_diff('month', DATE '{event}', period) AS rel_month,
               100.0 * sum(n_dlq60) / sum(n_loans) AS dlq60_pct,
               100.0 * sum(n_disaster_dlq) / sum(n_loans) AS disaster_dlq_pct,
               sum(n_loans) AS loan_months,
               count(DISTINCT zip3) AS zip3_areas
        FROM panel JOIN groups USING (zip3)
        WHERE period BETWEEN DATE '{event}' - INTERVAL {int(h["months_before"])} MONTH
                         AND DATE '{event}' + INTERVAL {int(h["months_after"])} MONTH
        GROUP BY grp, rel_month ORDER BY grp, rel_month
    """).fetchall()


def default_by_band(con: duckdb.DuckDBPyConnection, column: str, edges: list[float]) -> list:
    return con.execute(f"""
        SELECT {bin_case(column, edges)} AS band,
               count(*) AS loans, sum(default_24m) AS defaults,
               100.0 * avg(default_24m) AS default_rate_pct
        FROM loans WHERE in_pd_sample AND {column} IS NOT NULL
        GROUP BY band ORDER BY band
    """).fetchall()


def default_by_exposure(con: duckdb.DuckDBPyConnection, edges: list[float]) -> list[tuple]:
    """Default rate by ex-ante exposure, measured in the month before the first payment
    (proxy for the origination month; Freddie Mac does not publish the exact date)."""
    positive_edges = edges[1:]
    bands = " ".join(
        f"WHEN exposure_main <= {upper} THEN '{i + 1}: ({lower:g}, {upper:g}]'"
        for i, (lower, upper) in enumerate(zip(edges[:-1], positive_edges, strict=True))
    )
    return con.execute(f"""
        WITH x AS (
            SELECT l.default_24m, c.exposure_main
            FROM loans l
            JOIN climate c
              ON c.zip3 = l.zip3
             AND c.month = CAST(l.first_payment_date - INTERVAL 1 MONTH AS DATE)
            WHERE l.in_pd_sample
        )
        SELECT CASE WHEN exposure_main = 0 THEN '0: none'
                    {bands}
                    ELSE '{len(edges)}: >{edges[-1]:g}' END AS band,
               count(*) AS loans, sum(default_24m) AS defaults,
               100.0 * avg(default_24m) AS default_rate_pct
        FROM x GROUP BY band ORDER BY band
    """).fetchall()


# --- Figures --------------------------------------------------------------------------


def _save(fig, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    log.info("Saved %s", path)


def fig_default_by_vintage(rows: list[tuple], path: Path) -> None:
    """Bars: all labelled loans. Dots: PD modelling sample (the population the models use)."""
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch

    fig, ax = plt.subplots(figsize=(9, 4.4))
    vintages = [r[0] for r in rows]
    ax.bar(vintages, [r[3] for r in rows], color=COLORS["axis"], width=0.7, zorder=1)
    pd_rows = [r for r in rows if r[5] and r[6] is not None]
    ax.scatter(
        [r[0] for r in pd_rows],
        [r[6] for r in pd_rows],
        s=36,
        color=COLORS["focus"],
        edgecolors=COLORS["surface"],
        linewidths=1.5,
        zorder=3,
    )
    peak = max(pd_rows, key=lambda r: r[6])
    ax.annotate(
        f"{peak[6]:.1f}%",
        (peak[0], peak[6]),
        xytext=(8, 0),
        textcoords="offset points",
        va="center",
        color=COLORS["ink"],
    )
    ax.set_title(f"24-month default rate peaked at {peak[6]:.1f}% for {peak[0]} loans")
    ax.set_ylabel("Loans in default within 24 months (%)")
    ax.set_xticks(vintages, [str(v) if v % 2 == 0 else "" for v in vintages])
    ax.legend(
        handles=[
            Patch(color=COLORS["axis"], label="All labelled loans"),
            Line2D(
                [],
                [],
                marker="o",
                linestyle="",
                color=COLORS["focus"],
                label="PD modelling sample (D005)",
            ),
        ],
        loc="upper left",
        bbox_to_anchor=(0, -0.08),
        ncol=2,
        fontsize=8.5,
    )
    ax.text(
        0,
        -0.2,
        "No dot: vintage excluded (24-month window overlaps COVID forbearance). Default = 90+ "
        "days past due,\nREO or loss exit within 24 months of first payment; complete windows "
        "only. Source: Freddie Mac SFLLD.",
        va="top",
        transform=ax.transAxes,
        color=COLORS["muted"],
        fontsize=8,
    )
    _save(fig, path)


def fig_disasters_by_year(rows: list[tuple], path: Path) -> None:
    fig, ax = plt.subplots(figsize=(9, 4.2))
    years = [r[0] for r in rows]
    ax.bar(years, [r[1] for r in rows], color=COLORS["focus"], width=0.7)
    top = sorted(rows, key=lambda r: -r[1])[:3]
    for y, v in top:
        ax.annotate(
            str(y),
            (y, v),
            xytext=(0, 4),
            textcoords="offset points",
            ha="center",
            color=COLORS["ink"],
            fontsize=9,
        )
    ax.set_title(
        "Climate disasters with household assistance: "
        + ", ".join(str(y) for y, _ in sorted(top))
        + " stand out"
    )
    ax.set_ylabel("US homes in counties hit (%)")
    ax.text(
        0,
        -0.15,
        "Major disaster declarations (hurricane, tropical/coastal storm, "
        "typhoon, flood, levee break, fire)\nwith household assistance. Source: OpenFEMA.",
        va="top",
        transform=ax.transAxes,
        color=COLORS["muted"],
        fontsize=8,
    )
    _save(fig, path)


def fig_harvey(rows: list[tuple], path: Path) -> None:
    style = {
        "Houston area (hit by Harvey)": {"color": COLORS["focus"], "zorder": 3},
        "Rest of Texas (not hit)": {"color": COLORS["muted"], "zorder": 2},
    }
    fig, (top, bottom) = plt.subplots(
        2, 1, figsize=(9, 6.6), sharex=True, gridspec_kw={"height_ratios": [3, 2], "hspace": 0.25}
    )
    for g in sorted({r[0] for r in rows}):
        series = [r for r in rows if r[0] == g]
        x = [r[1] for r in series]
        counts = [r[5] for r in series]
        area_count = (
            str(min(counts)) if min(counts) == max(counts) else f"{min(counts)}-{max(counts)}"
        )
        label = f"{g}, {area_count} observed ZIP3"
        top.plot(x, [r[2] for r in series], label=label, **style.get(g, {}))
        bottom.plot(x, [r[3] for r in series], **style.get(g, {}))
    houston = [r for r in rows if r[0].startswith("Houston") and r[1] >= 0]
    control = {r[1]: r[2] for r in rows if r[0].startswith("Rest")}
    if houston:
        peak = max(houston, key=lambda r: r[2])
        top.annotate(
            f"{peak[2]:.1f}%",
            (peak[1], peak[2]),
            xytext=(6, 0),
            textcoords="offset points",
            va="center",
            color=COLORS["ink"],
        )
        ctl = control.get(peak[1])
        ctl_txt = f" (rest of Texas: {ctl:.1f}%)" if ctl is not None else ""
        top.set_title(
            f"Houston-area 60+ day delinquency peaked at {peak[2]:.1f}%, "
            f"{peak[1]} month{'s' if peak[1] != 1 else ''} after Harvey{ctl_txt}",
            fontsize=11,
        )
    for ax in (top, bottom):
        ax.axvline(0, color=COLORS["ink_secondary"], linewidth=0.8)
    top.annotate(
        "Harvey, Aug 2017",
        (0, 1),
        xycoords=("data", "axes fraction"),
        xytext=(-6, -2),
        textcoords="offset points",
        ha="right",
        va="top",
        color=COLORS["ink_secondary"],
        fontsize=8.5,
    )
    top.set_ylabel("Loans 60+ days past due (%)")
    bottom.set_ylabel("Flagged 'due to disaster' (%)")
    bottom.set_xlabel("Months relative to August 2017")
    top.legend(loc="upper right", fontsize=9)
    bottom.text(
        0,
        -0.42,
        "Raw rates, no controls. Treated: Texas ZIP3 with >= 90% main-definition impact "
        "in Aug 2017; "
        "includes the wider Gulf Coast.\nControl: no broad-definition impact within +/-3 months. "
        "Source: Freddie Mac SFLLD, OpenFEMA.",
        transform=bottom.transAxes,
        color=COLORS["muted"],
        fontsize=8,
    )
    _save(fig, path)


def _band_labels(rows: list) -> list[str]:
    return [str(r[0]).split(": ", 1)[-1] for r in rows]


def fig_default_by_fico_ltv(fico: list, ltv: list, path: Path) -> None:
    fig, (a, b) = plt.subplots(1, 2, figsize=(9, 3.8), sharey=True)
    for ax, rows, label in ((a, fico, "FICO score"), (b, ltv, "Loan-to-value (%)")):
        ax.bar(_band_labels(rows), [r[3] for r in rows], color=COLORS["focus"], width=0.6)
        ax.set_xlabel(label)
    a.set_ylabel("24-month default rate (%)")

    def trend(rows: list, name: str) -> str:
        lo, hi = rows[0][3], rows[-1][3]
        verb = "falls" if hi < lo else "rises"
        return f"{verb} with {name} ({lo:.1f}% to {hi:.1f}%)"

    fig.suptitle(
        f"Default rate {trend(fico, 'FICO')} and {trend(ltv, 'LTV')}",
        x=0.07,
        ha="left",
        fontweight="bold",
        fontsize=12,
    )
    a.text(
        0,
        -0.28,
        "PD modelling sample (D005). Source: Freddie Mac SFLLD.",
        transform=a.transAxes,
        color=COLORS["muted"],
        fontsize=8,
    )
    _save(fig, path)


def fig_default_by_exposure(rows: list, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.bar(_band_labels(rows), [r[3] for r in rows], color=COLORS["focus"], width=0.6)
    for i, r in enumerate(rows):
        ax.annotate(
            f"{r[1]:,} loans",
            (i, r[3]),
            xytext=(0, 4),
            textcoords="offset points",
            ha="center",
            color=COLORS["ink_secondary"],
            fontsize=8,
        )
    ax.set_title("Raw default rate by past climate-disaster exposure")
    ax.set_xlabel("Expected disasters per home in the previous 10 years")
    ax.set_ylabel("24-month default rate (%)")
    ax.text(
        0,
        -0.25,
        "Raw, no controls: exposure is correlated with region and vintage. "
        "Measured the month before first payment.",
        transform=ax.transAxes,
        color=COLORS["muted"],
        fontsize=8,
    )
    _save(fig, path)


# --- Report ---------------------------------------------------------------------------


def md_table(headers: list[str], rows: list, fmt: dict | None = None) -> str:
    fmt = fmt or {}

    def cell(i: int, v: object) -> str:
        if v is None:
            return ""
        if i in fmt:
            return fmt[i].format(v)
        return str(v)

    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(cell(i, v) for i, v in enumerate(r)) + " |" for r in rows]
    return "\n".join(lines)


def run(
    processed_dir: Path = PROCESSED_DIR,
    figures_dir: Path = FIGURES_DIR,
    report_path: Path = REPORT_PATH,
    cfg: dict | None = None,
    fema_path: Path = FEMA_PATH,
) -> Path:
    cfg = cfg or load_config()
    apply_style()
    con = duckdb.connect()
    for view, name in (
        ("loans", "loans"),
        ("panel", "zip3_month"),
        ("climate", "zip3_month_climate"),
        ("weights", "zip3_county_weights"),
    ):
        con.execute(
            f"CREATE VIEW {view} AS SELECT * FROM "
            f"read_parquet('{processed_dir / (name + '.parquet')}')"
        )

    con.execute(f"CREATE VIEW events AS {events_query(load_climate_config(), 'main', fema_path)}")
    vintage = default_by_vintage(con)
    disasters = disasters_by_year(con)
    harvey = harvey_series(con, cfg)
    fico = default_by_band(con, "fico", cfg["fico_bins"])
    ltv = default_by_band(con, "ltv", cfg["ltv_bins"])
    exposure = default_by_exposure(con, cfg["exposure_bins"])

    fig_default_by_vintage(vintage, figures_dir / "fig1_default_by_vintage.png")
    fig_disasters_by_year(disasters, figures_dir / "fig2_disasters_by_year.png")
    fig_harvey(harvey, figures_dir / "fig3_harvey_delinquency.png")
    fig_default_by_fico_ltv(fico, ltv, figures_dir / "fig4_default_by_fico_ltv.png")
    fig_default_by_exposure(exposure, figures_dir / "fig5_default_by_exposure.png")

    rel = Path("figures")
    pct = "{:.2f}"
    sections = [
        "# Exploratory analysis",
        "Generated by `python -m climrisk.viz.eda`. Raw descriptive patterns; "
        "causal estimates come from the event study (Phase 5).",
        "## 1. Default rate by vintage",
        f"![Default rate by vintage]({rel / 'fig1_default_by_vintage.png'})",
        md_table(
            [
                "vintage",
                "labelled",
                "defaults",
                "default_rate_pct",
                "covid_window",
                "pd_loans",
                "pd_default_rate_pct",
            ],
            vintage,
            {3: pct, 6: pct},
        ),
        "## 2. Climate disasters by year",
        f"![Disasters by year]({rel / 'fig2_disasters_by_year.png'})",
        md_table(["year", "homes_hit_pct"], disasters, {1: pct}),
        "## 3. Delinquency around Hurricane Harvey",
        f"![Harvey]({rel / 'fig3_harvey_delinquency.png'})",
        md_table(
            ["group", "rel_month", "dlq60_pct", "disaster_dlq_pct", "loan_months", "zip3_areas"],
            harvey,
            {2: pct, 3: pct},
        ),
        "## 4. Default rate by FICO and LTV (PD sample)",
        f"![FICO and LTV]({rel / 'fig4_default_by_fico_ltv.png'})",
        md_table(["fico_band", "loans", "defaults", "default_rate_pct"], fico, {3: pct}),
        md_table(["ltv_band", "loans", "defaults", "default_rate_pct"], ltv, {3: pct}),
        "## 5. Default rate by ex-ante climate exposure (PD sample, raw)",
        f"![Exposure]({rel / 'fig5_default_by_exposure.png'})",
        md_table(["exposure_band", "loans", "defaults", "default_rate_pct"], exposure, {3: pct}),
    ]
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n\n".join(sections) + "\n", encoding="utf-8")
    log.info("Wrote %s", report_path)
    con.close()
    return report_path


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()


if __name__ == "__main__":
    main()
