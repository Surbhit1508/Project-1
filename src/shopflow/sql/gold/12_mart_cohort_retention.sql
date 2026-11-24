-- gold.mart_cohort_retention: monthly acquisition cohorts (month of first purchase) and the share
-- of each cohort that purchases again N months later.
CREATE OR REPLACE TABLE gold.mart_cohort_retention AS
WITH activity AS (
    SELECT DISTINCT customer_id, CAST(date_trunc('month', order_ts) AS DATE) AS activity_month
    FROM gold.fact_orders
    WHERE status <> 'cancelled'
),
cohorts AS (
    SELECT customer_id, MIN(activity_month) AS cohort_month
    FROM activity
    GROUP BY customer_id
),
sizes AS (
    SELECT cohort_month, COUNT(*) AS cohort_size FROM cohorts GROUP BY cohort_month
)
SELECT
    c.cohort_month,
    date_diff('month', c.cohort_month, a.activity_month)          AS months_since_first,
    s.cohort_size,
    COUNT(DISTINCT a.customer_id)                                 AS active_customers,
    ROUND(COUNT(DISTINCT a.customer_id) / s.cohort_size, 4)       AS retention_rate
FROM activity a
JOIN cohorts c USING (customer_id)
JOIN sizes s   USING (cohort_month)
GROUP BY c.cohort_month, months_since_first, s.cohort_size
ORDER BY c.cohort_month, months_since_first;
