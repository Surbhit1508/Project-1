-- silver.customers_history: SCD Type 2 built from the customer CDC stream.
--
-- 1. type + normalise (emails are case-insensitive)
-- 2. drop exact re-deliveries of the same change
-- 3. change detection on tracked attributes: CRM "re-syncs" that change nothing do not open
--    a new version
-- 4. close each version at the start of the next one (half-open [valid_from, valid_to))
CREATE OR REPLACE TABLE silver.customers_history AS
WITH typed AS (
    SELECT
        TRIM(customer_id)                         AS customer_id,
        TRIM(first_name)                          AS first_name,
        TRIM(last_name)                           AS last_name,
        LOWER(TRIM(email))                        AS email,
        UPPER(TRIM(country))                      AS country,
        LOWER(TRIM(acquisition_channel))          AS acquisition_channel,
        TRY_CAST(birth_year AS INTEGER)           AS birth_year,
        TRY_CAST(signup_date AS DATE)             AS signup_date,
        LOWER(TRIM(loyalty_tier))                 AS loyalty_tier,
        TRY_CAST(updated_at AS TIMESTAMP)         AS updated_at,
        _ingested_at,
        _source_file
    FROM bronze.customers
    WHERE NULLIF(TRIM(customer_id), '') IS NOT NULL
),
dedup AS (
    SELECT * FROM typed
    WHERE updated_at IS NOT NULL
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY customer_id, updated_at ORDER BY _ingested_at DESC, _source_file DESC
    ) = 1
),
hashed AS (
    SELECT
        *,
        md5(concat_ws('|', first_name, last_name, email, country, acquisition_channel,
                      birth_year, signup_date, loyalty_tier)) AS attr_hash
    FROM dedup
),
changes AS (
    SELECT * FROM hashed
    QUALIFY attr_hash IS DISTINCT FROM
            LAG(attr_hash) OVER (PARTITION BY customer_id ORDER BY updated_at)
)
SELECT
    customer_id, first_name, last_name, email, country, acquisition_channel,
    birth_year, signup_date, loyalty_tier, attr_hash,
    updated_at                                                         AS valid_from,
    COALESCE(LEAD(updated_at) OVER w, TIMESTAMP '9999-12-31 00:00:00') AS valid_to,
    LEAD(updated_at) OVER w IS NULL                                    AS is_current,
    ROW_NUMBER() OVER w                                                AS version
FROM changes
WINDOW w AS (PARTITION BY customer_id ORDER BY updated_at);
