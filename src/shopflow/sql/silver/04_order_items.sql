-- silver.order_items: nested JSON line items flattened to one row per order line.
-- Lines with impossible values or unknown products are quarantined, not dropped.
CREATE OR REPLACE TEMP TABLE _items AS
WITH parsed AS (
    SELECT
        order_id,
        from_json(items, '[{"product_id": "VARCHAR", "quantity": "INTEGER", "unit_price": "DOUBLE"}]')
            AS item_list
    FROM silver.orders
),
exploded AS (
    SELECT order_id, item_list, UNNEST(generate_series(1, len(item_list))) AS line_number
    FROM parsed
)
SELECT
    e.order_id,
    CAST(e.line_number AS INTEGER)                              AS line_number,
    TRIM(e.item_list[e.line_number].product_id)                 AS product_id,
    e.item_list[e.line_number].quantity                         AS quantity,
    CAST(e.item_list[e.line_number].unit_price AS DECIMAL(10, 2)) AS unit_price,
    CASE
        WHEN e.item_list[e.line_number].product_id IS NULL      THEN 'missing_product_id'
        WHEN p.product_id IS NULL                               THEN 'unknown_product'
        WHEN e.item_list[e.line_number].quantity IS NULL
          OR e.item_list[e.line_number].quantity <= 0           THEN 'non_positive_quantity'
        WHEN e.item_list[e.line_number].unit_price IS NULL
          OR e.item_list[e.line_number].unit_price < 0          THEN 'invalid_unit_price'
    END AS reject_reason
FROM exploded e
LEFT JOIN silver.products p ON p.product_id = TRIM(e.item_list[e.line_number].product_id);

CREATE OR REPLACE TABLE silver.order_items_quarantine AS
SELECT * FROM _items WHERE reject_reason IS NOT NULL;

CREATE OR REPLACE TABLE silver.order_items AS
SELECT order_id, line_number, product_id, quantity, unit_price
FROM _items
WHERE reject_reason IS NULL;

DROP TABLE _items;
