-- gold.fact_order_line: grain = one product line of one order.
--
-- The customer join is POINT-IN-TIME: each order is attributed to the customer version that was
-- valid when it was placed (e.g. revenue from a customer's "silver" period stays "silver" after
-- they upgrade to "gold").
CREATE OR REPLACE TABLE gold.fact_order_line AS
SELECT
    i.order_id,
    i.line_number,
    CAST(strftime(o.order_ts, '%Y%m%d') AS INTEGER)          AS date_key,
    COALESCE(c.customer_sk, 0)                               AS customer_sk,
    p.product_sk,
    o.customer_id,
    i.product_id,
    o.order_ts,
    o.status,
    o.channel,
    i.quantity,
    i.unit_price,
    CAST(i.quantity * i.unit_price AS DECIMAL(12, 2))        AS gross_amount,
    CAST(i.quantity * p.unit_cost AS DECIMAL(12, 2))         AS cost_amount,
    CAST(CASE WHEN o.status = 'completed'
              THEN i.quantity * i.unit_price ELSE 0 END AS DECIMAL(12, 2)) AS net_revenue,
    CAST(CASE WHEN o.status = 'completed'
              THEN i.quantity * (i.unit_price - p.unit_cost) ELSE 0 END AS DECIMAL(12, 2))
                                                             AS gross_margin
FROM silver.order_items i
JOIN silver.orders o USING (order_id)
JOIN gold.dim_product p ON p.product_id = i.product_id
LEFT JOIN gold.dim_customer c
       ON c.customer_id = o.customer_id
      AND o.order_ts >= c.valid_from
      AND o.order_ts <  c.valid_to;
