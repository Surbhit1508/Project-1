-- gold.fact_orders: grain = one order (header level), aggregated from order lines.
CREATE OR REPLACE TABLE gold.fact_orders AS
WITH lines AS (
    SELECT
        order_id,
        ANY_VALUE(customer_sk)       AS customer_sk,
        COUNT(*)                     AS line_count,
        SUM(quantity)                AS units,
        SUM(gross_amount)            AS items_amount,
        SUM(net_revenue)             AS net_revenue,
        SUM(gross_margin)            AS gross_margin
    FROM gold.fact_order_line
    GROUP BY order_id
)
SELECT
    o.order_id,
    CAST(strftime(o.order_ts, '%Y%m%d') AS INTEGER)         AS date_key,
    COALESCE(l.customer_sk, c.customer_sk, 0)               AS customer_sk,
    o.customer_id,
    o.order_ts,
    o.status,
    o.returned_at,
    o.channel,
    o.payment_method,
    o.discount_code,
    o.discount_code IS NOT NULL                             AS has_discount,
    COALESCE(l.line_count, 0)                               AS line_count,
    COALESCE(l.units, 0)                                    AS units,
    COALESCE(l.items_amount, 0)                             AS items_amount,
    o.shipping_fee,
    COALESCE(l.items_amount, 0) + o.shipping_fee            AS order_total,
    COALESCE(l.net_revenue, 0)                              AS net_revenue,
    COALESCE(l.gross_margin, 0)                             AS gross_margin
FROM silver.orders o
LEFT JOIN lines l USING (order_id)
LEFT JOIN gold.dim_customer c
       ON c.customer_id = o.customer_id
      AND o.order_ts >= c.valid_from
      AND o.order_ts <  c.valid_to;
