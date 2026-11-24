"""Silver and gold behave correctly on the injected data problems."""

from __future__ import annotations

from shopflow.warehouse import scalar


def _quarantined(con, table, reason):
    return scalar(con, f"SELECT COUNT(*) FROM {table} WHERE reject_reason = ?", [reason])


def test_every_injected_problem_is_quarantined_exactly_once(built, con):
    injected = built[2].injected
    assert _quarantined(con, "silver.orders_quarantine", "missing_customer_id") == \
        injected["missing_customer_id"]
    assert _quarantined(con, "silver.orders_quarantine", "unparseable_order_ts") == \
        injected["unparseable_order_ts"]
    assert _quarantined(con, "silver.order_items_quarantine", "non_positive_quantity") == \
        injected["negative_quantity"]
    assert _quarantined(con, "silver.order_items_quarantine", "unknown_product") == \
        injected["unknown_product"]


def test_messy_status_is_normalised_not_rejected(con):
    assert scalar(con, "SELECT COUNT(*) FROM silver.orders_quarantine "
                       "WHERE reject_reason = 'invalid_status'") == 0
    assert scalar(con, "SELECT COUNT(DISTINCT status) FROM silver.orders") == 3


def test_duplicates_collapse_and_latest_status_wins(con):
    assert scalar(con, "SELECT COUNT(*) - COUNT(DISTINCT order_id) FROM silver.orders") == 0
    # every order that ever received a 'returned' event ends up returned in silver
    wrong = scalar(con, """
        SELECT COUNT(*) FROM silver.orders o
        WHERE o.order_id IN (SELECT order_id FROM bronze.orders
                             WHERE LOWER(TRIM(status)) = 'returned')
          AND o.status <> 'returned'
    """)
    assert wrong == 0


def test_scd2_ignores_noop_resyncs_and_normalises_email(con):
    bronze = scalar(con, "SELECT COUNT(*) FROM bronze.customers")
    versions = scalar(con, "SELECT COUNT(*) FROM silver.customers_history")
    assert versions < bronze
    assert scalar(con, "SELECT COUNT(*) FROM silver.customers_history "
                       "WHERE email <> LOWER(email)") == 0
    # consecutive versions of a customer always differ in some attribute
    assert scalar(con, """
        SELECT COUNT(*) FROM (
            SELECT attr_hash = LAG(attr_hash) OVER (PARTITION BY customer_id ORDER BY version) AS same
            FROM silver.customers_history) WHERE same
    """) == 0


def test_fact_rows_join_the_customer_version_valid_at_order_time(con):
    mismatches = scalar(con, """
        SELECT COUNT(*) FROM gold.fact_order_line f
        JOIN gold.dim_customer c USING (customer_sk)
        WHERE f.customer_sk <> 0
          AND NOT (f.order_ts >= c.valid_from AND f.order_ts < c.valid_to
                   AND f.customer_id = c.customer_id)
    """)
    assert mismatches == 0
    # and tier upgrades are visible historically: some customers have orders under >1 tier
    assert scalar(con, """
        SELECT COUNT(*) FROM (
            SELECT f.customer_id FROM gold.fact_order_line f
            JOIN gold.dim_customer c USING (customer_sk)
            GROUP BY 1 HAVING COUNT(DISTINCT c.loyalty_tier) > 1)
    """) > 0


def test_revenue_is_conserved_from_silver_to_marts(con):
    silver = scalar(con, """
        SELECT SUM(i.quantity * i.unit_price) FROM silver.order_items i
        JOIN silver.orders o USING (order_id) WHERE o.status = 'completed'
    """)
    mart = scalar(con, "SELECT SUM(net_revenue) FROM gold.mart_daily_kpis")
    cat = scalar(con, "SELECT SUM(net_revenue) FROM gold.mart_category_monthly")
    assert abs(silver - mart) < 0.01
    assert abs(silver - cat) < 0.01


def test_rfm_scores_are_quintiles(con):
    rows = con.execute("SELECT MIN(r_score), MAX(r_score), MIN(f_score), MAX(f_score) "
                       "FROM gold.mart_customer_rfm").fetchall()[0]
    assert rows == (1, 5, 1, 5)
