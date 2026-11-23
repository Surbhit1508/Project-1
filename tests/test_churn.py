from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from shopflow.ml.churn import evaluate, plan_cutoffs
from shopflow.ml.features import FEATURES, build_features


def test_cutoff_plan_has_no_label_overlap():
    train, test = plan_cutoffs(date(2024, 1, 1), date(2025, 6, 30), horizon_days=90)
    assert test == date(2025, 4, 1)
    assert all(c + timedelta(days=90) <= test for c in train)
    assert train == sorted(train) and len(train) >= 3


def test_plan_cutoffs_rejects_short_history():
    with pytest.raises(ValueError):
        plan_cutoffs(date(2024, 1, 1), date(2024, 5, 1))


def test_features_do_not_leak_the_future(con):
    """Deleting everything that happened after the cutoff must not change any feature."""
    cutoff = date(2024, 8, 1)
    before = build_features(con, cutoff).drop(columns=["churned"])
    con.execute("BEGIN TRANSACTION")
    try:
        con.execute("DELETE FROM gold.fact_order_line WHERE order_ts >= ?", [cutoff])
        con.execute("DELETE FROM gold.fact_orders WHERE order_ts >= ?", [cutoff])
        # returns processed after the cutoff were not known at the cutoff
        con.execute("UPDATE gold.fact_orders SET status = 'completed', returned_at = NULL "
                    "WHERE returned_at >= ?", [cutoff])
        con.execute("DELETE FROM gold.dim_customer WHERE valid_from >= ?", [cutoff])
        con.execute("UPDATE gold.dim_customer SET valid_to = TIMESTAMP '9999-12-31' "
                    "WHERE valid_to >= ? AND customer_sk <> 0", [cutoff])
        after = build_features(con, cutoff).drop(columns=["churned"])
    finally:
        con.execute("ROLLBACK")
    pd.testing.assert_frame_equal(before, after)


def test_feature_frame_shape(con):
    df = build_features(con, date(2024, 9, 1))
    assert set(FEATURES) <= set(df.columns)
    assert df["customer_id"].is_unique
    assert df["recency_days"].between(0, 180).all()  # active population only
    assert set(df["churned"].unique()) <= {0, 1}


def test_evaluate_on_perfect_and_random_scores():
    rng = np.random.default_rng(0)
    y = rng.integers(0, 2, 2000)
    perfect = evaluate(y, y.astype(float))
    assert perfect["roc_auc"] == 1.0 and perfect["precision_top_decile"] == 1.0
    rand = evaluate(y, rng.random(2000))
    assert 0.45 < rand["roc_auc"] < 0.55
    assert 0.8 < rand["lift_top_decile"] < 1.2


def test_training_and_scoring_outputs(built, con):
    paths = built[0]
    assert (paths.artifacts / "churn_model.joblib").exists()
    metrics = built[3].outputs["train_churn_model"].metrics
    best = built[3].outputs["train_churn_model"].best_model
    assert metrics[best]["roc_auc"] > 0.65
    p = con.execute("SELECT MIN(churn_probability), MAX(churn_probability), COUNT(*), "
                    "COUNT(DISTINCT customer_id) FROM gold.churn_scores").fetchall()[0]
    assert 0 <= p[0] <= p[1] <= 1 and p[2] == p[3] > 0


def test_report_is_written(built):
    paths = built[0]
    report = (paths.reports / "REPORT.md").read_text()
    assert "## Churn risk" in report and "figures/churn_deciles.png" in report
    assert (paths.reports / "MODEL_CARD.md").exists()
    for fig in ["monthly_revenue", "cohort_retention", "rfm_segments", "churn_deciles"]:
        assert (paths.reports / "figures" / f"{fig}.png").stat().st_size > 10_000
