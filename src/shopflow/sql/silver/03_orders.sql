-- silver.orders: one row per order in its latest known state.
--
-- * TRY_CAST instead of CAST: a bad value must not crash the load, it must be quarantined.
-- * Latest-state wins: orders are re-sent when their status changes (e.g. returned), and some
--   deliveries are exact duplicates. Rank by updated_at, then ingestion order.
-- * returned_at keeps the time of the status change so downstream features can be computed
--   "as of" a past date without leaking future returns.
-- * Rows failing validation go to silver.orders_quarantine with a reason instead of being
--   silently dropped.
CREATE OR REPLACE TEMP TABLE _orders_latest AS
WITH typed AS (
    SELECT
        NULLIF(TRIM(order_id), '')                      AS order_id,
        NULLIF(TRIM(customer_id), '')                   AS customer_id,
        TRY_CAST(order_ts AS TIMESTAMP)                 AS order_ts,
        TRY_CAST(updated_at AS TIMESTAMP)               AS updated_at,
        LOWER(TRIM(status))                             AS status,
        LOWER(TRIM(channel))                            AS channel,
        LOWER(TRIM(payment_method))                     AS payment_method,
        NULLIF(UPPER(TRIM(discount_code)), '')          AS discount_code,
        UPPER(TRIM(currency))                           AS currency,
        TRY_CAST(shipping_fee AS DECIMAL(10, 2))        AS shipping_fee,
        items,
        order_ts                                        AS raw_order_ts,
        _source_file,
        _ingested_at
    FROM bronze.orders
)
SELECT
    *,
    CASE
        WHEN order_id IS NULL                                     THEN 'missing_order_id'
        WHEN customer_id IS NULL                                  THEN 'missing_customer_id'
        WHEN order_ts IS NULL                                     THEN 'unparseable_order_ts'
        WHEN status NOT IN ('completed', 'cancelled', 'returned') THEN 'invalid_status'
        WHEN currency <> 'USD'                                    THEN 'unsupported_currency'
        WHEN shipping_fee IS NULL OR shipping_fee < 0             THEN 'invalid_shipping_fee'
        WHEN json_array_length(items) = 0                         THEN 'no_items'
    END AS reject_reason
FROM typed
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY order_id
    ORDER BY updated_at DESC NULLS LAST, _ingested_at DESC, _source_file DESC
) = 1;

CREATE OR REPLACE TABLE silver.orders_quarantine AS
SELECT order_id, customer_id, raw_order_ts, status, reject_reason, _source_file, _ingested_at
FROM _orders_latest
WHERE reject_reason IS NOT NULL;

CREATE OR REPLACE TABLE silver.orders AS
SELECT
    order_id, customer_id, order_ts, updated_at, status,
    CASE WHEN status = 'returned' THEN updated_at END AS returned_at,
    channel, payment_method, discount_code, currency, shipping_fee, items,
    _source_file, _ingested_at
FROM _orders_latest
WHERE reject_reason IS NULL;

DROP TABLE _orders_latest;
