"""Point-in-time customer features for churn modelling.

For a ``cutoff`` timestamp every feature is computed **only from information available before
the cutoff**:

* orders placed before the cutoff,
* order status *as it was known* at the cutoff (an order returned after the cutoff still counts
  as completed - using its final status would leak the future),
* the customer attributes from the SCD2 version that was valid at the cutoff.

The label looks forward: a customer has churned if they place no (non-cancelled) order in the
``horizon_days`` after the cutoff.

Population: customers who are still *active* at the cutoff (an order in the last
``active_within_days``). Customers who lapsed long ago are trivially "churned" - including them
inflates every metric and is not who a retention team can save (that is a win-back problem).
"""

from __future__ import annotations

from datetime import date, datetime

import duckdb
import pandas as pd

NUMERIC_FEATURES = [
    "recency_days",
    "tenure_days",
    "days_since_signup",
    "n_orders",
    "orders_last_30d",
    "orders_last_90d",
    "orders_prev_90d",
    "order_trend_90d",
    "monetary_total",
    "monetary_last_90d",
    "avg_order_value",
    "avg_units_per_order",
    "return_rate",
    "cancel_rate",
    "discount_rate",
    "distinct_categories",
    "mean_days_between_orders",
    "overdue_ratio",
    "mobile_share",
]
CATEGORICAL_FEATURES = ["loyalty_tier", "acquisition_channel", "country"]
FEATURES = NUMERIC_FEATURES + CATEGORICAL_FEATURES
TARGET = "churned"

FEATURE_SQL = """
WITH params AS (
    SELECT CAST($cutoff AS TIMESTAMP) AS cutoff, CAST($horizon AS INTEGER) AS horizon,
           CAST($active_within AS INTEGER) AS active_within
),
hist AS (
    SELECT
        f.*,
        CASE WHEN f.status = 'returned' AND f.returned_at >= p.cutoff THEN 'completed'
             ELSE f.status END AS status_asof,
        date_diff('day', f.order_ts, p.cutoff) AS age_days
    FROM gold.fact_orders f, params p
    WHERE f.order_ts < p.cutoff
),
agg AS (
    SELECT
        customer_id,
        MIN(age_days) FILTER (WHERE status_asof <> 'cancelled')                 AS recency_days,
        MAX(age_days) FILTER (WHERE status_asof <> 'cancelled')                 AS tenure_days,
        COUNT(*) FILTER (WHERE status_asof <> 'cancelled')                      AS n_orders,
        COUNT(*) FILTER (WHERE status_asof <> 'cancelled' AND age_days < 30)    AS orders_last_30d,
        COUNT(*) FILTER (WHERE status_asof <> 'cancelled' AND age_days < 90)    AS orders_last_90d,
        COUNT(*) FILTER (WHERE status_asof <> 'cancelled'
                           AND age_days >= 90 AND age_days < 180)               AS orders_prev_90d,
        COALESCE(SUM(items_amount) FILTER (WHERE status_asof = 'completed'), 0) AS monetary_total,
        COALESCE(SUM(items_amount) FILTER (WHERE status_asof = 'completed'
                                             AND age_days < 90), 0)             AS monetary_last_90d,
        AVG(units) FILTER (WHERE status_asof <> 'cancelled')                    AS avg_units_per_order,
        AVG(CASE WHEN status_asof = 'returned' THEN 1.0 ELSE 0 END)
            FILTER (WHERE status_asof <> 'cancelled')                           AS return_rate,
        AVG(CASE WHEN status_asof = 'cancelled' THEN 1.0 ELSE 0 END)            AS cancel_rate,
        AVG(CASE WHEN has_discount THEN 1.0 ELSE 0 END)                         AS discount_rate,
        AVG(CASE WHEN channel IN ('ios', 'android') THEN 1.0 ELSE 0 END)        AS mobile_share
    FROM hist
    GROUP BY customer_id
    HAVING COUNT(*) FILTER (WHERE status_asof <> 'cancelled') > 0
       AND MIN(age_days) FILTER (WHERE status_asof <> 'cancelled')
           < ANY_VALUE((SELECT active_within FROM params))
),
gaps AS (
    SELECT customer_id, AVG(gap_days) AS mean_days_between_orders
    FROM (
        SELECT customer_id,
               date_diff('hour', LAG(order_ts) OVER (PARTITION BY customer_id ORDER BY order_ts),
                         order_ts) / 24.0 AS gap_days
        FROM hist WHERE status_asof <> 'cancelled'
    )
    WHERE gap_days IS NOT NULL
    GROUP BY customer_id
),
cats AS (
    SELECT l.customer_id, COUNT(DISTINCT p.category) AS distinct_categories
    FROM gold.fact_order_line l
    JOIN gold.dim_product p USING (product_sk), params pr
    WHERE l.order_ts < pr.cutoff AND l.status <> 'cancelled'
    GROUP BY l.customer_id
),
attrs AS (
    SELECT c.customer_id, c.loyalty_tier, c.acquisition_channel, c.country, c.signup_date
    FROM gold.dim_customer c, params p
    WHERE c.customer_sk <> 0 AND c.valid_from < p.cutoff AND c.valid_to >= p.cutoff
),
future AS (
    SELECT DISTINCT f.customer_id
    FROM gold.fact_orders f, params p
    WHERE f.order_ts >= p.cutoff
      AND f.order_ts < p.cutoff + to_days(p.horizon)
      AND f.status <> 'cancelled'
)
SELECT
    a.customer_id,
    CAST((SELECT cutoff FROM params) AS DATE)                           AS cutoff,
    a.recency_days,
    a.tenure_days,
    date_diff('day', t.signup_date, CAST((SELECT cutoff FROM params) AS DATE)) AS days_since_signup,
    a.n_orders,
    a.orders_last_30d,
    a.orders_last_90d,
    a.orders_prev_90d,
    a.orders_last_90d - a.orders_prev_90d                               AS order_trend_90d,
    CAST(a.monetary_total AS DOUBLE)                                    AS monetary_total,
    CAST(a.monetary_last_90d AS DOUBLE)                                 AS monetary_last_90d,
    CAST(a.monetary_total AS DOUBLE) / a.n_orders                       AS avg_order_value,
    a.avg_units_per_order,
    a.return_rate,
    a.cancel_rate,
    a.discount_rate,
    COALESCE(c.distinct_categories, 0)                                  AS distinct_categories,
    g.mean_days_between_orders,
    a.recency_days / NULLIF(g.mean_days_between_orders, 0)              AS overdue_ratio,
    a.mobile_share,
    COALESCE(t.loyalty_tier, 'unknown')                                 AS loyalty_tier,
    COALESCE(t.acquisition_channel, 'unknown')                          AS acquisition_channel,
    COALESCE(t.country, 'UNKNOWN')                                      AS country,
    CASE WHEN fu.customer_id IS NULL THEN 1 ELSE 0 END                  AS churned
FROM agg a
LEFT JOIN gaps g   USING (customer_id)
LEFT JOIN cats c   USING (customer_id)
LEFT JOIN attrs t  USING (customer_id)
LEFT JOIN future fu USING (customer_id)
ORDER BY a.customer_id
"""


def build_features(con: duckdb.DuckDBPyConnection, cutoff: date | datetime,
                   horizon_days: int = 90, active_within_days: int = 180) -> pd.DataFrame:
    """Feature matrix (+ label) for every customer active in the ``active_within_days`` before
    ``cutoff``.

    The label is only meaningful if ``cutoff + horizon_days`` is within the observed data;
    when scoring "today" it is simply ignored.
    """
    df = con.execute(FEATURE_SQL, {"cutoff": cutoff, "horizon": horizon_days,
                                   "active_within": active_within_days}).df()
    for col in NUMERIC_FEATURES:
        df[col] = df[col].astype("float64")
    for col in CATEGORICAL_FEATURES:
        df[col] = df[col].astype("string")
    return df
