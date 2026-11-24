-- gold.dim_customer: SCD2 customer dimension with surrogate keys.
-- customer_sk = 0 is the "unknown member" used for late-arriving / missing customers, so facts
-- never drop rows because a dimension record has not arrived yet.
CREATE OR REPLACE TABLE gold.dim_customer AS
SELECT
    0                                   AS customer_sk,
    'UNKNOWN'                           AS customer_id,
    NULL::VARCHAR                       AS first_name,
    NULL::VARCHAR                       AS last_name,
    NULL::VARCHAR                       AS email,
    'UNKNOWN'                           AS country,
    'unknown'                           AS acquisition_channel,
    NULL::INTEGER                       AS birth_year,
    NULL::DATE                          AS signup_date,
    'unknown'                           AS loyalty_tier,
    TIMESTAMP '1900-01-01 00:00:00'     AS valid_from,
    TIMESTAMP '9999-12-31 00:00:00'     AS valid_to,
    TRUE                                AS is_current,
    0                                   AS version
UNION ALL
SELECT
    ROW_NUMBER() OVER (ORDER BY customer_id, valid_from) AS customer_sk,
    customer_id, first_name, last_name, email, country, acquisition_channel,
    birth_year, signup_date, loyalty_tier, valid_from, valid_to, is_current, version
FROM silver.customers_history;
