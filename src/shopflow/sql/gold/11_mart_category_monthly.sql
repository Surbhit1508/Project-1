-- gold.mart_category_monthly: revenue, margin and volume per category per month.
CREATE OR REPLACE TABLE gold.mart_category_monthly AS
SELECT
    d.month_start,
    p.category,
    COUNT(DISTINCT f.order_id) FILTER (WHERE f.status = 'completed')     AS orders,
    SUM(f.quantity) FILTER (WHERE f.status = 'completed')                AS units,
    SUM(f.net_revenue)                                                   AS net_revenue,
    SUM(f.gross_margin)                                                  AS gross_margin,
    ROUND(SUM(f.gross_margin) / NULLIF(SUM(f.net_revenue), 0), 4)        AS margin_pct,
    ROUND(COUNT(*) FILTER (WHERE f.status = 'returned')
          / NULLIF(COUNT(*) FILTER (WHERE f.status IN ('completed', 'returned')), 0), 4)
                                                                         AS line_return_rate
FROM gold.fact_order_line f
JOIN gold.dim_date d    USING (date_key)
JOIN gold.dim_product p USING (product_sk)
GROUP BY d.month_start, p.category
ORDER BY d.month_start, p.category;
