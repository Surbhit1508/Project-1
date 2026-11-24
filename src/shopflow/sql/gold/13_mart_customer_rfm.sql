-- gold.mart_customer_rfm: Recency / Frequency / Monetary scoring and segmentation, as of the day
-- after the latest order in the warehouse.
CREATE OR REPLACE TABLE gold.mart_customer_rfm AS
WITH as_of AS (
    SELECT CAST(MAX(order_ts) AS DATE) + 1 AS as_of_date FROM gold.fact_orders
),
base AS (
    SELECT
        f.customer_id,
        date_diff('day', CAST(MAX(f.order_ts) AS DATE), ANY_VALUE(a.as_of_date)) AS recency_days,
        COUNT(*)                                                               AS frequency,
        SUM(f.net_revenue)                                                     AS monetary,
        MIN(f.order_ts)                                                        AS first_order_ts,
        MAX(f.order_ts)                                                        AS last_order_ts
    FROM gold.fact_orders f, as_of a
    WHERE f.status <> 'cancelled'
    GROUP BY f.customer_id
),
scored AS (
    SELECT
        *,
        NTILE(5) OVER (ORDER BY recency_days DESC) AS r_score,
        NTILE(5) OVER (ORDER BY frequency, monetary) AS f_score,
        NTILE(5) OVER (ORDER BY monetary)          AS m_score
    FROM base
)
SELECT
    s.customer_id,
    (SELECT as_of_date FROM as_of)                  AS as_of_date,
    s.recency_days, s.frequency, s.monetary, s.first_order_ts, s.last_order_ts,
    s.r_score, s.f_score, s.m_score,
    CONCAT(s.r_score, s.f_score, s.m_score)         AS rfm_code,
    CASE
        WHEN s.r_score >= 4 AND s.f_score >= 4 THEN 'Champions'
        WHEN s.r_score >= 3 AND s.f_score >= 3 THEN 'Loyal'
        WHEN s.r_score >= 4                    THEN 'New & Promising'
        WHEN s.r_score <= 2 AND s.f_score >= 3 THEN 'At Risk'
        WHEN s.r_score = 3                     THEN 'Needs Attention'
        ELSE 'Hibernating'
    END                                             AS segment,
    c.country,
    c.loyalty_tier,
    c.acquisition_channel
FROM scored s
LEFT JOIN gold.dim_customer c ON c.customer_id = s.customer_id AND c.is_current;
