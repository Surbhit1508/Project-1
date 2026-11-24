"""Builds ``reports/REPORT.md`` (business + pipeline report) and ``reports/MODEL_CARD.md``.

Charts are static PNGs so the report renders anywhere (GitHub, a PR, an email).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import duckdb
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402

from .config import Paths  # noqa: E402
from .warehouse import scalar, table_exists  # noqa: E402

# Validated reference palette (light mode) - see the dataviz palette reference.
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e3df"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]
NEUTRAL = "#a3a29c"
BLUE_RAMP = ["#f4f8fe", "#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95",
             "#0d366b"]


def _style() -> None:
    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "axes.edgecolor": GRID, "axes.labelcolor": INK_2, "axes.titlecolor": INK,
        "axes.titlesize": 13, "axes.titleweight": "bold", "axes.titlelocation": "left",
        "axes.titlepad": 12, "axes.spines.top": False, "axes.spines.right": False,
        "axes.spines.left": False, "axes.grid": True, "axes.axisbelow": True,
        "grid.color": GRID, "grid.linewidth": 0.8, "xtick.color": INK_2, "ytick.color": INK_2,
        "xtick.major.size": 0, "ytick.major.size": 0, "font.size": 10.5,
        "legend.frameon": False, "legend.labelcolor": INK_2,
    })


def _money(x: float) -> str:
    if abs(x) >= 1e6:
        return f"${x / 1e6:.2f}M"
    if abs(x) >= 1e3:
        return f"${x / 1e3:.1f}k"
    return f"${x:,.0f}"


def _md_table(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    lines += ["| " + " | ".join(str(v) for v in row) + " |" for row in df.itertuples(index=False)]
    return "\n".join(lines)


# ------------------------------------------------------------------------------------ charts
def _chart_monthly_revenue(con, out: Path) -> None:
    df = con.execute("""
        SELECT month_start, SUM(net_revenue)::DOUBLE AS revenue
        FROM gold.mart_category_monthly GROUP BY 1 ORDER BY 1
    """).df()
    fig, ax = plt.subplots(figsize=(9, 3.8))
    x = np.arange(len(df))
    ax.bar(x, df["revenue"], width=0.72, color=SERIES[0], edgecolor=SURFACE, linewidth=2)
    ax.set_xticks(x, [d.strftime("%b\n%Y") if d.month == 1 or i == 0 else d.strftime("%b")
                      for i, d in enumerate(pd.to_datetime(df["month_start"]))])
    ax.yaxis.set_major_formatter(lambda v, _: _money(v))
    ax.grid(axis="x", visible=False)
    peak = int(df["revenue"].idxmax())
    ax.annotate(_money(df["revenue"][peak]), (peak, df["revenue"][peak]),
                xytext=(0, 4), textcoords="offset points", ha="center", color=INK, fontsize=9.5)
    ax.set_title("Net revenue per month")
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)


def _chart_cohorts(con, out: Path) -> None:
    df = con.execute("""
        SELECT cohort_month, months_since_first, retention_rate
        FROM gold.mart_cohort_retention WHERE months_since_first BETWEEN 1 AND 12
    """).df()
    grid = df.pivot(index="cohort_month", columns="months_since_first", values="retention_rate")
    grid = grid.sort_index()
    fig, ax = plt.subplots(figsize=(9, 5.2))
    cmap = LinearSegmentedColormap.from_list("blue", BLUE_RAMP)
    im = ax.imshow(grid.to_numpy(dtype=float), cmap=cmap, vmin=0, vmax=0.7, aspect="auto")
    ax.set_xticks(range(grid.shape[1]), [str(c) for c in grid.columns])
    ax.set_yticks(range(grid.shape[0]),
                  [pd.Timestamp(d).strftime("%b %Y") for d in grid.index])
    ax.set_xlabel("Months since first purchase")
    ax.grid(False)
    for (i, j), v in np.ndenumerate(grid.to_numpy(dtype=float)):
        if not np.isnan(v):
            ax.text(j, i, f"{v:.0%}", ha="center", va="center", fontsize=7.5,
                    color="white" if v > 0.4 else INK)
    cb = fig.colorbar(im, ax=ax, fraction=0.03, pad=0.02, format=lambda v, _: f"{v:.0%}")
    cb.outline.set_visible(False)
    cb.ax.tick_params(colors=INK_2)
    ax.set_title("Cohort retention: share of each first-purchase cohort buying again")
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)


def _chart_segments(con, out: Path) -> None:
    df = con.execute("""
        SELECT segment, COUNT(*) AS customers, SUM(monetary)::DOUBLE AS revenue
        FROM gold.mart_customer_rfm GROUP BY 1 ORDER BY revenue
    """).df()
    share = df["revenue"] / df["revenue"].sum()
    fig, ax = plt.subplots(figsize=(9, 3.6))
    y = np.arange(len(df))
    ax.barh(y, share, height=0.66, color=SERIES[0], edgecolor=SURFACE, linewidth=2)
    ax.set_yticks(y, df["segment"])
    ax.xaxis.set_major_formatter(lambda v, _: f"{v:.0%}")
    ax.grid(axis="y", visible=False)
    for yi, s, n in zip(y, share, df["customers"], strict=True):
        ax.text(s + 0.006, yi, f"{s:.0%} of revenue · {n:,} customers", va="center",
                color=INK_2, fontsize=9)
    ax.set_xlim(0, share.max() * 1.45)
    ax.set_title("Lifetime revenue by RFM segment")
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)


def _chart_deciles(con, out: Path) -> None:
    """Observed churn rate per predicted-risk decile (ranking) vs mean prediction (calibration)."""
    df = con.execute("""
        SELECT decile, AVG(churned) AS observed, AVG(churn_probability) AS predicted
        FROM (SELECT *, NTILE(10) OVER (ORDER BY churn_probability DESC) AS decile
              FROM meta.churn_test_predictions)
        GROUP BY decile ORDER BY decile
    """).df()
    base = scalar(con, "SELECT AVG(churned) FROM meta.churn_test_predictions")
    fig, ax = plt.subplots(figsize=(9, 4))
    x = df["decile"].to_numpy()
    ax.bar(x, df["observed"], width=0.7, color=SERIES[0], edgecolor=SURFACE, linewidth=2,
           label="Observed churn rate")
    ax.plot(x, df["predicted"], linestyle="none", marker="o", markersize=8, color=SERIES[1],
            markeredgecolor=SURFACE, markeredgewidth=2, label="Mean predicted probability")
    ax.axhline(base, color=NEUTRAL, linewidth=1.2, linestyle=(0, (4, 3)))
    ax.text(10.45, base, f"average\n{base:.0%}", color=INK_2, fontsize=9, va="center")
    for xi, v in zip(x[[0, -1]], df["observed"].to_numpy()[[0, -1]], strict=True):
        ax.annotate(f"{v:.0%}", (xi, v), xytext=(0, 4), textcoords="offset points", ha="center",
                    color=INK, fontsize=9.5)
    ax.set_xticks(x, ["1\nhighest risk"] + [str(i) for i in x[1:-1]] + ["10\nlowest risk"])
    ax.set_xlabel("Predicted-risk decile (out-of-time test month)")
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0%}")
    ax.set_ylim(0, 1.08)
    ax.set_xlim(0.4, 11.2)
    ax.grid(axis="x", visible=False)
    ax.legend(loc="upper right", bbox_to_anchor=(0.97, 1.0))
    ax.set_title("Churn rate by predicted-risk decile")
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)


# ----------------------------------------------------------------------------------- report
def build(con: duckdb.DuckDBPyConnection, paths: Paths, run_id: str | None = None) -> Path:
    _style()
    fig_dir = paths.reports / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)
    has_ml = table_exists(con, "gold.churn_scores")

    _chart_monthly_revenue(con, fig_dir / "monthly_revenue.png")
    _chart_cohorts(con, fig_dir / "cohort_retention.png")
    _chart_segments(con, fig_dir / "rfm_segments.png")
    if has_ml:
        _chart_deciles(con, fig_dir / "churn_deciles.png")

    k = con.execute("""
        SELECT MIN(date), MAX(date), SUM(net_revenue)::DOUBLE, SUM(gross_margin)::DOUBLE,
               SUM(completed_orders), SUM(returned_orders), SUM(orders)
        FROM gold.mart_daily_kpis
    """).fetchall()[0]
    first, last, revenue, margin, completed, returned, orders = k
    customers = scalar(con, "SELECT COUNT(*) FROM gold.mart_customer_rfm")
    l30, p30 = con.execute("""
        WITH k AS (SELECT *, date_diff('day', date, (SELECT MAX(date) FROM gold.mart_daily_kpis)) AS age
                   FROM gold.mart_daily_kpis)
        SELECT SUM(net_revenue) FILTER (WHERE age < 30)::DOUBLE,
               SUM(net_revenue) FILTER (WHERE age >= 30 AND age < 60)::DOUBLE
        FROM k
    """).fetchall()[0]

    # Data-quality results of the most recent run that executed checks.
    latest_dq = "(SELECT run_id FROM meta.dq_results ORDER BY checked_at DESC LIMIT 1)"
    dq = warn_rows = None
    if table_exists(con, "meta.dq_results"):
        dq = con.execute(f"""
            SELECT layer, COUNT(*) AS checks, COUNT(*) FILTER (WHERE passed) AS passed,
                   COUNT(*) FILTER (WHERE NOT passed AND severity = 'warn') AS warnings,
                   COUNT(*) FILTER (WHERE NOT passed AND severity = 'error') AS errors
            FROM meta.dq_results WHERE run_id = {latest_dq} GROUP BY layer ORDER BY layer DESC
        """).df()
        warn_rows = con.execute(f"""
            SELECT check_name, severity, failures, description FROM meta.dq_results
            WHERE run_id = {latest_dq} AND NOT passed ORDER BY check_name
        """).df()
    quarantine = con.execute("""
        SELECT 'orders' AS entity, reject_reason AS reason, COUNT(*) AS rows
        FROM silver.orders_quarantine GROUP BY ALL
        UNION ALL
        SELECT 'order lines', reject_reason, COUNT(*) FROM silver.order_items_quarantine GROUP BY ALL
        ORDER BY rows DESC
    """).df()
    tables = con.execute("""
        SELECT schema_name, table_name FROM duckdb_tables()
        WHERE schema_name IN ('bronze', 'silver', 'gold')
        ORDER BY CASE schema_name WHEN 'bronze' THEN 1 WHEN 'silver' THEN 2 ELSE 3 END, table_name
    """).fetchall()
    layers = pd.DataFrame(
        [[sch, f"`{sch}.{tbl}`", f"{scalar(con, f'SELECT COUNT(*) FROM {sch}.{tbl}'):,}"]
         for sch, tbl in tables],
        columns=["layer", "table", "rows"],
    )
    cats = con.execute("""
        SELECT category,
               SUM(net_revenue)::DOUBLE AS rev, SUM(gross_margin)::DOUBLE AS margin,
               SUM(units) AS units,
               AVG(line_return_rate) AS rr
        FROM gold.mart_category_monthly
        WHERE month_start > (SELECT MAX(month_start) FROM gold.mart_category_monthly) - INTERVAL 12 MONTH
        GROUP BY 1 ORDER BY rev DESC
    """).df()
    cats_md = pd.DataFrame({
        "category": cats["category"],
        "net revenue (12 mo)": cats["rev"].map(_money),
        "gross margin": (cats["margin"] / cats["rev"]).map("{:.1%}".format),
        "units": cats["units"].map("{:,.0f}".format),
        "avg line return rate": cats["rr"].map("{:.1%}".format),
    })

    md = [
        "# ShopFlow: analytics report",
        "",
        f"_Generated {datetime.now():%Y-%m-%d %H:%M} from `data/warehouse.duckdb`"
        + (f" (run `{run_id}`)" if run_id else "") + "._",
        "",
        "## Headline numbers",
        "",
        _md_table(pd.DataFrame([
            ["Period covered", f"{first:%d %b %Y} to {last:%d %b %Y}"],
            ["Net revenue", _money(revenue)],
            ["Gross margin", f"{_money(margin)} ({margin / revenue:.1%})"],
            ["Orders (excl. cancelled)", f"{orders:,}"],
            ["Customers with a purchase", f"{customers:,}"],
            ["Average order value", f"${revenue / completed:,.2f}"],
            ["Return rate", f"{returned / (completed + returned):.1%}"],
            ["Net revenue, last 30 days vs prior 30", f"{_money(l30)} ({(l30 / p30 - 1):+.1%})"],
        ], columns=["Metric", "Value"])),
        "",
        "![Monthly revenue](figures/monthly_revenue.png)",
        "",
        "### Category performance (last 12 months)",
        "",
        _md_table(cats_md),
        "",
        "## Customers",
        "",
        "![Cohort retention](figures/cohort_retention.png)",
        "",
        "![RFM segments](figures/rfm_segments.png)",
        "",
    ]

    if has_ml:
        m = json.loads((paths.artifacts / "churn_metrics.json").read_text())
        best = m["metrics"][m["best_model"]]
        base = m["metrics"]["baseline_recency"]
        bands = con.execute("""
            SELECT risk_band AS "risk band", COUNT(*) AS customers,
                   ROUND(AVG(churn_probability), 3) AS "avg churn prob.",
                   ROUND(SUM(revenue_at_risk_90d), 0) AS "revenue at risk (90d)"
            FROM gold.churn_scores GROUP BY 1
            ORDER BY CASE risk_band WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END
        """).df()
        bands["revenue at risk (90d)"] = bands["revenue at risk (90d)"].map(_money)
        top = con.execute("""
            SELECT customer_id, ROUND(churn_probability, 2) AS "churn prob.",
                   rfm_segment AS "RFM segment", loyalty_tier AS tier,
                   recency_days::INT AS "days since last order",
                   printf('$%,.0f', monetary_last_90d) AS "spend, last 90d",
                   printf('$%,.0f', revenue_at_risk_90d) AS "expected revenue at risk"
            FROM gold.churn_scores ORDER BY revenue_at_risk_90d DESC LIMIT 10
        """).df()
        md += [
            "## Churn risk",
            "",
            f"Model: **{m['best_model'].replace('_', ' ')}**, trained on {m['n_train']:,} "
            f"customer-month snapshots ({m['train_cutoffs'][0]} to {m['train_cutoffs'][-1]}) and "
            f"evaluated out-of-time on {m['n_test']:,} active customers at {m['test_cutoff']} "
            f"(churn = no order in the next {m['horizon_days']} days). "
            "See [MODEL_CARD.md](MODEL_CARD.md).",
            "",
            _md_table(pd.DataFrame([
                ["ROC-AUC", f"{best['roc_auc']:.3f}", f"{base['roc_auc']:.3f}"],
                ["PR-AUC", f"{best['pr_auc']:.3f}", f"{base['pr_auc']:.3f}"],
                ["Precision in top 10%", f"{best['precision_top_decile']:.1%}",
                 f"{base['precision_top_decile']:.1%}"],
                ["Lift in top 10%", f"{best['lift_top_decile']:.2f}x",
                 f"{base['lift_top_decile']:.2f}x"],
            ], columns=["Test metric", "Model", "Recency baseline"])),
            "",
            "![Churn rate by risk decile](figures/churn_deciles.png)",
            "",
            "### Current risk bands",
            "",
            _md_table(bands),
            "",
            "### Retention call list: highest expected revenue at risk",
            "",
            _md_table(top),
            "",
        ]

    md += ["## Pipeline health", ""]
    if dq is not None and len(dq):
        md += ["### Data-quality gates", "", _md_table(dq), ""]
        if warn_rows is not None and len(warn_rows):
            md += ["Non-passing checks:", "", _md_table(warn_rows), ""]
    md += [
        "### Records quarantined in silver",
        "",
        "Rows that failed validation are kept with a reason instead of being silently dropped.",
        "",
        _md_table(quarantine),
        "",
        "### Warehouse tables",
        "",
        _md_table(layers),
        "",
    ]
    out = paths.reports / "REPORT.md"
    out.write_text("\n".join(md), encoding="utf-8")
    if has_ml:
        _model_card(paths)
    return out


def _model_card(paths: Paths) -> None:
    m = json.loads((paths.artifacts / "churn_metrics.json").read_text())
    rows = [[name.replace("_", " "), f"{v['roc_auc']:.3f}", f"{v['pr_auc']:.3f}",
             "-" if v.get("brier") is None or v["brier"] != v["brier"] else f"{v['brier']:.3f}",
             f"{v['precision_top_decile']:.1%}", f"{v['lift_top_decile']:.2f}x"]
            for name, v in m["metrics"].items()]
    imp = [[f, f"{v:+.4f}"] for f, v in m["permutation_importance"][:10]]
    text = f"""# Model card: customer churn

## Purpose
Rank currently active customers by their probability of placing **no order in the next
{m['horizon_days']} days**, so a retention team can spend a limited budget (vouchers,
outreach) where it matters. Output: `gold.churn_scores` (one row per active customer, with
risk band and expected revenue at risk).

## Data
* Population: customers with at least one non-cancelled order in the 180 days before the cutoff.
* Features: {len(m['permutation_importance'])} point-in-time features: recency, frequency,
  monetary, trend, cadence ("how overdue is this customer relative to their own rhythm"),
  returns/cancellations/discount usage, category breadth, device mix, and the SCD2 customer
  attributes valid at the cutoff. Order status is taken *as known at the cutoff* (a return
  processed later does not leak back).
* Training set: monthly snapshots {m['train_cutoffs'][0]} to {m['train_cutoffs'][-1]}
  ({m['n_train']:,} rows). Every training label window closes before the test cutoff.
* Test set: out-of-time snapshot at {m['test_cutoff']} ({m['n_test']:,} customers).

## Results (out-of-time test)

{_md_table(pd.DataFrame(rows, columns=["model", "ROC-AUC", "PR-AUC", "Brier", "precision@10%", "lift@10%"]))}

Selected model: **{m['best_model'].replace('_', ' ')}** (highest PR-AUC).

### Permutation importance (drop in PR-AUC when the feature is shuffled)

{_md_table(pd.DataFrame(imp, columns=["feature", "importance"]))}

## Interpretation and caveats
* **Recency is a strong baseline.** In non-contractual retail "days since last order" alone is
  very predictive. The model's value is in the *ranking among recently active customers*:
  cadence, trend and bad experiences (returns) separate a quiet regular from a fading one.
* **Seasonality / drift.** The churn base rate differs by cutoff month (holiday months pull
  customers back). Retrain monthly and monitor the base rate and PR-AUC per scoring run.
* **Correlated rows.** A customer appears in several monthly snapshots. That is fine for
  training but means the effective sample size is smaller than the row count.
* **Not causal.** A high score says who is likely to leave, not who will respond to an
  intervention; an uplift model (with a randomised holdout) is the next step.
* Data is synthetic; numbers illustrate the method, not a real business.
"""
    (paths.reports / "MODEL_CARD.md").write_text(text, encoding="utf-8")
