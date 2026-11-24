"""Churn model: training with an out-of-time split, evaluation, and batch scoring.

Validation design
-----------------
Training examples are monthly snapshots (one row per active customer per cutoff). The test set
is the **most recent** cutoff whose 90-day label window is fully observed. Training cutoffs are
restricted to those whose *label window ends before the test cutoff*, so nothing the model is
trained on was observed after the moment it is evaluated at. This is the same constraint the
model faces in production, and it is the reason we do not use a random train/test split.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import duckdb
import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, OrdinalEncoder, StandardScaler

from ..warehouse import scalar
from .features import CATEGORICAL_FEATURES, FEATURES, NUMERIC_FEATURES, TARGET, build_features

log = logging.getLogger(__name__)

MODEL_FILE = "churn_model.joblib"
METRICS_FILE = "churn_metrics.json"
RISK_BANDS = ((0.6, "high"), (0.35, "medium"), (0.0, "low"))


# ------------------------------------------------------------------------------ data split
def plan_cutoffs(first_order: date, last_order: date, horizon_days: int = 90,
                 min_history_days: int = 90) -> tuple[list[date], date]:
    """Monthly training cutoffs and the out-of-time test cutoff.

    test cutoff  = last month start whose label window ends on/before ``last_order``
    train cutoffs = month starts with >= ``min_history_days`` of history whose label window
                    closes on/before the test cutoff (no overlap with the test period)
    """
    month_starts = pd.date_range(first_order + timedelta(days=min_history_days),
                                 last_order, freq="MS").date
    observable = [d for d in month_starts if d + timedelta(days=horizon_days) <= last_order]
    if len(observable) < 2:
        raise ValueError("not enough history to build a train/test split")
    test_cutoff = observable[-1]
    train = [d for d in observable if d + timedelta(days=horizon_days) <= test_cutoff]
    if not train:
        raise ValueError("no training cutoff leaves a gap before the test cutoff")
    return train, test_cutoff


# ------------------------------------------------------------------------------- the models
def _logistic_regression() -> Pipeline:
    pre = ColumnTransformer([
        ("num", Pipeline([("impute", SimpleImputer(strategy="median", add_indicator=True)),
                          ("scale", StandardScaler())]), NUMERIC_FEATURES),
        ("cat", OneHotEncoder(handle_unknown="ignore"), CATEGORICAL_FEATURES),
    ])
    return Pipeline([("pre", pre),
                     ("clf", LogisticRegression(max_iter=2000, C=0.5))])


def _gradient_boosting(seed: int) -> Pipeline:
    pre = ColumnTransformer([
        ("cat", OrdinalEncoder(handle_unknown="use_encoded_value", unknown_value=np.nan,
                               encoded_missing_value=np.nan), CATEGORICAL_FEATURES),
        ("num", "passthrough", NUMERIC_FEATURES),
    ])
    clf = HistGradientBoostingClassifier(
        learning_rate=0.05, max_iter=400, max_leaf_nodes=15, min_samples_leaf=40,
        l2_regularization=1.0, early_stopping=True, validation_fraction=0.15,
        n_iter_no_change=30, categorical_features=list(range(len(CATEGORICAL_FEATURES))),
        random_state=seed,
    )
    return Pipeline([("pre", pre), ("clf", clf)])


def evaluate(y: np.ndarray, p: np.ndarray, top_frac: float = 0.1) -> dict[str, float]:
    """Ranking + calibration metrics, plus the business view: precision/lift in the top decile."""
    y, p = np.asarray(y), np.asarray(p)
    k = max(1, int(round(len(p) * top_frac)))
    top = np.argsort(-p, kind="stable")[:k]
    base = float(y.mean())
    precision_top = float(y[top].mean())
    return {
        "roc_auc": float(roc_auc_score(y, p)),
        "pr_auc": float(average_precision_score(y, p)),
        "brier": float(brier_score_loss(y, p)) if p.min() >= 0 and p.max() <= 1 else float("nan"),
        "base_rate": base,
        "precision_top_decile": precision_top,
        "lift_top_decile": precision_top / base if base else float("nan"),
        "recall_top_decile": float(y[top].sum() / max(y.sum(), 1)),
    }


@dataclass
class TrainResult:
    best_model: str
    metrics: dict[str, dict[str, float]]
    train_cutoffs: list[date]
    test_cutoff: date
    n_train: int
    n_test: int
    feature_importance: list[tuple[str, float]]
    model_path: Path


def train(con: duckdb.DuckDBPyConnection, artifacts: Path, horizon_days: int = 90,
          seed: int = 42) -> TrainResult:
    first, last = con.execute(
        "SELECT CAST(MIN(order_ts) AS DATE), CAST(MAX(order_ts) AS DATE) FROM gold.fact_orders"
    ).fetchall()[0]
    train_cutoffs, test_cutoff = plan_cutoffs(first, last, horizon_days)
    log.info("  train cutoffs %s..%s (%d), test cutoff %s", train_cutoffs[0],
             train_cutoffs[-1], len(train_cutoffs), test_cutoff)

    train_df = pd.concat([build_features(con, c, horizon_days) for c in train_cutoffs],
                         ignore_index=True)
    test_df = build_features(con, test_cutoff, horizon_days)
    x_tr, y_tr = train_df[FEATURES], train_df[TARGET].to_numpy()
    x_te, y_te = test_df[FEATURES], test_df[TARGET].to_numpy()
    log.info("  %d training rows (churn rate %.1f%%), %d test rows (churn rate %.1f%%)",
             len(x_tr), 100 * y_tr.mean(), len(x_te), 100 * y_te.mean())

    metrics: dict[str, dict[str, float]] = {
        # Baseline every model must beat: "the longer since the last order, the likelier churn".
        "baseline_recency": evaluate(y_te, x_te["recency_days"].fillna(1e6).to_numpy()),
    }
    metrics["baseline_recency"]["brier"] = float("nan")
    candidates = {
        "logistic_regression": _logistic_regression(),
        "gradient_boosting": _gradient_boosting(seed),
    }
    fitted = {}
    for name, model in candidates.items():
        model.fit(x_tr, y_tr)
        fitted[name] = model
        metrics[name] = evaluate(y_te, model.predict_proba(x_te)[:, 1])
        log.info("  %-22s ROC-AUC %.3f  PR-AUC %.3f  lift@10%% %.2f", name,
                 metrics[name]["roc_auc"], metrics[name]["pr_auc"],
                 metrics[name]["lift_top_decile"])
    log.info("  %-22s ROC-AUC %.3f  PR-AUC %.3f", "baseline_recency",
             metrics["baseline_recency"]["roc_auc"], metrics["baseline_recency"]["pr_auc"])

    best = max(candidates, key=lambda n: metrics[n]["pr_auc"])
    model = fitted[best]
    imp = permutation_importance(model, x_te, y_te, scoring="average_precision", n_repeats=5,
                                 random_state=seed, n_jobs=1)
    importance = sorted(zip(FEATURES, imp.importances_mean.round(4).tolist(), strict=True),
                        key=lambda t: -t[1])

    artifacts.mkdir(parents=True, exist_ok=True)
    model_path = artifacts / MODEL_FILE
    joblib.dump({"model": model, "name": best, "features": FEATURES,
                 "horizon_days": horizon_days, "trained_through": str(test_cutoff)}, model_path)
    result = TrainResult(best, metrics, train_cutoffs, test_cutoff, len(x_tr), len(x_te),
                         importance, model_path)
    (artifacts / METRICS_FILE).write_text(json.dumps({
        "best_model": best,
        "horizon_days": horizon_days,
        "train_cutoffs": [str(c) for c in train_cutoffs],
        "test_cutoff": str(test_cutoff),
        "n_train": len(x_tr),
        "n_test": len(x_te),
        "metrics": metrics,
        "permutation_importance": importance,
    }, indent=2, default=str))
    _save_eval_curves(con, test_df, model.predict_proba(x_te)[:, 1])
    return result


def _save_eval_curves(con: duckdb.DuckDBPyConnection, test_df: pd.DataFrame,
                      proba: np.ndarray) -> None:
    """Persist test-set predictions so the report can draw gains/calibration charts."""
    out = pd.DataFrame({"customer_id": test_df["customer_id"], "cutoff": test_df["cutoff"],
                        "churned": test_df[TARGET], "churn_probability": proba,
                        "recency_days": test_df["recency_days"]})
    con.register("_eval", out)
    con.execute("CREATE OR REPLACE TABLE meta.churn_test_predictions AS SELECT * FROM _eval")
    con.unregister("_eval")


def score(con: duckdb.DuckDBPyConnection, artifacts: Path) -> int:
    """Score every customer as of the day after the last order -> ``gold.churn_scores``."""
    bundle = joblib.load(artifacts / MODEL_FILE)
    as_of = scalar(con, "SELECT CAST(MAX(order_ts) AS DATE) + 1 FROM gold.fact_orders")
    df = build_features(con, as_of, bundle["horizon_days"])
    df["churn_probability"] = bundle["model"].predict_proba(df[bundle["features"]])[:, 1]
    df["risk_band"] = pd.cut(df["churn_probability"], bins=[-0.01, 0.35, 0.6, 1.0],
                             labels=["low", "medium", "high"]).astype("string")
    # Expected revenue lost over the next 90 days if nothing is done.
    df["revenue_at_risk_90d"] = (df["churn_probability"] * df["monetary_last_90d"]).round(2)
    out = df[["customer_id", "cutoff", "churn_probability", "risk_band", "recency_days",
              "n_orders", "monetary_total", "monetary_last_90d", "revenue_at_risk_90d",
              "loyalty_tier", "country"]].rename(columns={"cutoff": "as_of_date"})
    out = out.assign(model=bundle["name"])
    con.register("_scores", out)
    con.execute("""
        CREATE OR REPLACE TABLE gold.churn_scores AS
        SELECT s.*, r.segment AS rfm_segment
        FROM _scores s
        LEFT JOIN gold.mart_customer_rfm r USING (customer_id)
        ORDER BY revenue_at_risk_90d DESC
    """)
    con.unregister("_scores")
    return len(out)
