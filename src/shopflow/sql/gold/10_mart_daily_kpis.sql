-- gold.mart_daily_kpis: the daily business scorecard.
-- Net revenue = item revenue of orders that were completed and not returned (excl. shipping).
CREATE OR REPLACE TABLE gold.mart_daily_kpis AS
WITH first_order AS (
    SELECT customer_id, CAST(MIN(order_ts) AS DATE) AS first_order_date
    FROM gold.fact_orders
    WHERE status <> 'cancelled'
    GROUP BY customer_id
),
daily AS (
    SELECT
        d.date,
        d.is_weekend,
        COUNT(f.order_id) FILTER (WHERE f.status <> 'cancelled')                AS orders,
        COUNT(f.order_id) FILTER (WHERE f.status = 'completed')                 AS completed_orders,
        COUNT(f.order_id) FILTER (WHERE f.status = 'returned')                  AS returned_orders,
        COUNT(f.order_id) FILTER (WHERE f.status = 'cancelled')                 AS cancelled_orders,
        COUNT(DISTINCT f.customer_id) FILTER (WHERE f.status <> 'cancelled')    AS active_customers,
        COUNT(DISTINCT f.customer_id) FILTER (WHERE fo.first_order_date = d.date) AS new_customers,
        COALESCE(SUM(f.net_revenue), 0)                                         AS net_revenue,
        COALESCE(SUM(f.gross_margin), 0)                                        AS gross_margin,
        COALESCE(SUM(f.units) FILTER (WHERE f.status = 'completed'), 0)         AS units_sold
    FROM gold.dim_date d
    LEFT JOIN gold.fact_orders f ON f.date_key = d.date_key
    LEFT JOIN first_order fo ON fo.customer_id = f.customer_id
    GROUP BY d.date, d.is_weekend
)
SELECT
    *,
    ROUND(net_revenue / NULLIF(completed_orders, 0), 2)                         AS avg_order_value,
    ROUND(returned_orders / NULLIF(completed_orders + returned_orders, 0), 4)   AS return_rate,
    ROUND(AVG(net_revenue) OVER (ORDER BY date ROWS BETWEEN 6 PRECEDING AND CURRENT ROW), 2)
                                                                                AS net_revenue_7d_avg
FROM daily
ORDER BY date;
