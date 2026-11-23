"""Declarative data-quality checks that gate the pipeline.

Every check is a SQL query that returns the *violating* rows (the same convention dbt tests
use). A check passes when the number of violations is at most ``max_failures``. ``error``
checks stop the pipeline before bad data is published to the next layer; ``warn`` checks are
recorded but do not block. All results are persisted in ``meta.dq_results`` for auditing and
trend monitoring.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

import duckdb

from .warehouse import scalar

log = logging.getLogger(__name__)

Severity = Literal["error", "warn"]


@dataclass(frozen=True)
class Check:
    name: str
    layer: str
    sql: str
    severity: Severity = "error"
    max_failures: int = 0
    description: str = ""


@dataclass(frozen=True)
class CheckResult:
    check: Check
    failures: int

    @property
    def passed(self) -> bool:
        return self.failures <= self.check.max_failures


class DataQualityError(RuntimeError):
    def __init__(self, failed: Sequence[CheckResult]):
        names = ", ".join(f"{r.check.name} ({r.failures})" for r in failed)
        super().__init__(f"{len(failed)} blocking data-quality check(s) failed: {names}")
        self.failed = list(failed)


# --------------------------------------------------------------------------------- builders
def not_null(layer: str, table: str, column: str, **kw) -> Check:
    return Check(f"{table}.{column}__not_null", layer,
                 f"SELECT * FROM {table} WHERE {column} IS NULL",
                 description=f"{column} is never null", **kw)


def unique(layer: str, table: str, *columns: str, **kw) -> Check:
    cols = ", ".join(columns)
    return Check(f"{table}.{'_'.join(columns)}__unique", layer,
                 f"SELECT {cols}, COUNT(*) AS n FROM {table} GROUP BY {cols} HAVING COUNT(*) > 1",
                 description=f"({cols}) is unique", **kw)


def accepted_values(layer: str, table: str, column: str, values: Iterable[str], **kw) -> Check:
    vals = ", ".join(f"'{v}'" for v in values)
    return Check(f"{table}.{column}__accepted_values", layer,
                 f"SELECT * FROM {table} WHERE {column} NOT IN ({vals})",
                 description=f"{column} in ({vals})", **kw)


def relationship(layer: str, table: str, column: str, ref_table: str, ref_column: str,
                 **kw) -> Check:
    return Check(f"{table}.{column}__references__{ref_table}", layer,
                 f"SELECT t.* FROM {table} t LEFT JOIN {ref_table} r "
                 f"ON t.{column} = r.{ref_column} WHERE r.{ref_column} IS NULL",
                 description=f"every {column} exists in {ref_table}.{ref_column}", **kw)


def expression(layer: str, table: str, name: str, condition: str, **kw) -> Check:
    return Check(f"{table}__{name}", layer,
                 f"SELECT * FROM {table} WHERE NOT ({condition})",
                 description=condition, **kw)


# ----------------------------------------------------------------------------- the contracts
SILVER_CHECKS: tuple[Check, ...] = (
    not_null("silver", "silver.orders", "order_id"),
    unique("silver", "silver.orders", "order_id"),
    not_null("silver", "silver.orders", "customer_id"),
    accepted_values("silver", "silver.orders", "status", ["completed", "cancelled", "returned"]),
    accepted_values("silver", "silver.orders", "channel", ["web", "ios", "android"]),
    expression("silver", "silver.orders", "returned_after_ordered",
               "returned_at IS NULL OR returned_at >= order_ts"),
    unique("silver", "silver.order_items", "order_id", "line_number"),
    expression("silver", "silver.order_items", "positive_quantity", "quantity > 0"),
    relationship("silver", "silver.order_items", "product_id", "silver.products", "product_id"),
    relationship("silver", "silver.order_items", "order_id", "silver.orders", "order_id"),
    unique("silver", "silver.products", "product_id"),
    expression("silver", "silver.products", "price_above_cost", "unit_price > unit_cost"),
    Check("silver.customers_history__one_current_version", "silver",
          "SELECT customer_id FROM silver.customers_history GROUP BY customer_id "
          "HAVING COUNT(*) FILTER (WHERE is_current) <> 1",
          description="exactly one current SCD2 version per customer"),
    Check("silver.customers_history__no_overlapping_versions", "silver",
          "SELECT a.customer_id FROM silver.customers_history a "
          "JOIN silver.customers_history b ON a.customer_id = b.customer_id "
          "AND a.version < b.version AND a.valid_to > b.valid_from",
          description="SCD2 validity intervals never overlap"),
    Check("silver.orders__quarantine_rate_below_2pct", "silver",
          "SELECT * FROM (SELECT (SELECT COUNT(*) FROM silver.orders_quarantine) * 1.0 / "
          "NULLIF((SELECT COUNT(*) FROM silver.orders_quarantine) + "
          "(SELECT COUNT(*) FROM silver.orders), 0) AS rate) WHERE rate > 0.02",
          description="<2% of orders are quarantined (spike = upstream incident)"),
    Check("silver.order_items__quarantine_rate_below_1pct", "silver",
          "SELECT * FROM (SELECT (SELECT COUNT(*) FROM silver.order_items_quarantine) * 1.0 / "
          "NULLIF((SELECT COUNT(*) FROM silver.order_items_quarantine) + "
          "(SELECT COUNT(*) FROM silver.order_items), 0) AS rate) WHERE rate > 0.01",
          severity="warn", description="<1% of order lines are quarantined"),
)

GOLD_CHECKS: tuple[Check, ...] = (
    unique("gold", "gold.fact_order_line", "order_id", "line_number"),
    unique("gold", "gold.fact_orders", "order_id"),
    unique("gold", "gold.dim_customer", "customer_sk"),
    relationship("gold", "gold.fact_order_line", "date_key", "gold.dim_date", "date_key"),
    relationship("gold", "gold.fact_order_line", "customer_sk", "gold.dim_customer", "customer_sk"),
    Check("gold.fact_order_line__revenue_reconciles_with_silver", "gold",
          "SELECT * FROM (SELECT "
          "(SELECT SUM(quantity * unit_price) FROM silver.order_items) AS silver_amt, "
          "(SELECT SUM(gross_amount) FROM gold.fact_order_line) AS gold_amt) "
          "WHERE ABS(silver_amt - gold_amt) > 0.01",
          description="no revenue gained or lost between silver and gold"),
    Check("gold.fact_orders__row_count_matches_silver", "gold",
          "SELECT * FROM (SELECT (SELECT COUNT(*) FROM silver.orders) AS s, "
          "(SELECT COUNT(*) FROM gold.fact_orders) AS g) WHERE s <> g",
          description="fact_orders has exactly one row per valid order"),
    Check("gold.fact_orders__unknown_customer_rate_below_1pct", "gold",
          "SELECT * FROM (SELECT AVG(CASE WHEN customer_sk = 0 THEN 1.0 ELSE 0 END) AS rate "
          "FROM gold.fact_orders) WHERE rate > 0.01",
          severity="warn", description="<1% of orders hit the unknown-customer member"),
    Check("gold.mart_daily_kpis__no_gap_days", "gold",
          "SELECT * FROM gold.mart_daily_kpis WHERE orders = 0",
          severity="warn", description="every calendar day has at least one order"),
    expression("gold", "gold.fact_order_line", "margin_not_above_revenue",
               "gross_margin <= net_revenue OR net_revenue = 0"),
)

CHECKS_BY_LAYER = {"silver": SILVER_CHECKS, "gold": GOLD_CHECKS}


def _ensure_table(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS meta.dq_results (
            run_id VARCHAR, checked_at TIMESTAMP, layer VARCHAR, check_name VARCHAR,
            severity VARCHAR, failures BIGINT, max_failures BIGINT, passed BOOLEAN,
            description VARCHAR
        )
        """
    )


def run_checks(con: duckdb.DuckDBPyConnection, checks: Iterable[Check],
               run_id: str = "adhoc") -> list[CheckResult]:
    _ensure_table(con)
    results = []
    for check in checks:
        failures = scalar(con, f"SELECT COUNT(*) FROM ({check.sql})")
        result = CheckResult(check, int(failures))
        results.append(result)
        level = logging.INFO if result.passed else (
            logging.ERROR if check.severity == "error" else logging.WARNING)
        log.log(level, "  [%s] %-62s failures=%d", "PASS" if result.passed else
                check.severity.upper(), check.name, result.failures)
    con.executemany(
        "INSERT INTO meta.dq_results VALUES (?, current_timestamp, ?, ?, ?, ?, ?, ?, ?)",
        [[run_id, r.check.layer, r.check.name, r.check.severity, r.failures,
          r.check.max_failures, r.passed, r.check.description] for r in results],
    )
    return results


def enforce(results: Sequence[CheckResult]) -> None:
    """Raise if any blocking (error-severity) check failed."""
    failed = [r for r in results if not r.passed and r.check.severity == "error"]
    if failed:
        raise DataQualityError(failed)
