-- gold.dim_date: calendar dimension covering every day with activity.
CREATE OR REPLACE TABLE gold.dim_date AS
WITH bounds AS (
    SELECT CAST(MIN(order_ts) AS DATE) AS lo, CAST(MAX(order_ts) AS DATE) AS hi
    FROM silver.orders
),
days AS (
    SELECT CAST(UNNEST(generate_series(CAST(lo AS TIMESTAMP), CAST(hi AS TIMESTAMP),
                                       INTERVAL 1 DAY)) AS DATE) AS date
    FROM bounds
)
SELECT
    CAST(strftime(date, '%Y%m%d') AS INTEGER)   AS date_key,
    date,
    year(date)                                  AS year,
    quarter(date)                               AS quarter,
    month(date)                                 AS month,
    monthname(date)                             AS month_name,
    day(date)                                   AS day_of_month,
    isodow(date)                                AS iso_day_of_week,
    dayname(date)                               AS day_name,
    isodow(date) IN (6, 7)                      AS is_weekend,
    CAST(date_trunc('week', date) AS DATE)      AS week_start,
    CAST(date_trunc('month', date) AS DATE)     AS month_start
FROM days
ORDER BY date;
