-- silver.products: typed, de-duplicated product catalogue (latest delivery wins).
CREATE OR REPLACE TABLE silver.products AS
SELECT
    TRIM(product_id)                          AS product_id,
    TRIM(product_name)                        AS product_name,
    LOWER(TRIM(category))                     AS category,
    TRY_CAST(unit_price AS DECIMAL(10, 2))    AS unit_price,
    TRY_CAST(unit_cost  AS DECIMAL(10, 2))    AS unit_cost
FROM bronze.products
WHERE NULLIF(TRIM(product_id), '') IS NOT NULL
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY TRIM(product_id) ORDER BY _ingested_at DESC, _source_file DESC
) = 1;
