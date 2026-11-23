-- gold.dim_product: product dimension with surrogate keys and price bands.
CREATE OR REPLACE TABLE gold.dim_product AS
SELECT
    ROW_NUMBER() OVER (ORDER BY product_id) AS product_sk,
    product_id,
    product_name,
    category,
    unit_price,
    unit_cost,
    ROUND((unit_price - unit_cost) / NULLIF(unit_price, 0), 4) AS list_margin_pct,
    CASE
        WHEN unit_price < 25  THEN 'budget'
        WHEN unit_price < 100 THEN 'mid'
        ELSE 'premium'
    END AS price_band
FROM silver.products;
