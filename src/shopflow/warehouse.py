"""DuckDB connection helpers and a tiny SQL-file runner."""

from __future__ import annotations

import logging
import re
from importlib import resources
from pathlib import Path

import duckdb

log = logging.getLogger(__name__)

SCHEMAS = ("bronze", "silver", "gold", "meta")


def connect(path: Path | str) -> duckdb.DuckDBPyConnection:
    """Open the warehouse and make sure the medallion schemas exist."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    for schema in SCHEMAS:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    return con


def scalar(con: duckdb.DuckDBPyConnection, sql: str, params=None):
    """Run a query and return the first column of its first row (consuming the full result)."""
    rows = con.execute(sql, params or []).fetchall()
    return rows[0][0] if rows else None


def sql_files(layer: str) -> list[tuple[str, str]]:
    """Return ``(name, sql)`` for every model in ``sql/<layer>`` in execution order."""
    folder = resources.files("shopflow") / "sql" / layer
    files = sorted(f for f in folder.iterdir() if f.name.endswith(".sql"))
    return [(f.name, f.read_text(encoding="utf-8")) for f in files]


def run_layer(con: duckdb.DuckDBPyConnection, layer: str) -> dict[str, int]:
    """Execute every model of a layer and return the row count of each table it built."""
    built: dict[str, int] = {}
    for name, sql in sql_files(layer):
        log.info("  %s/%s", layer, name)
        try:
            con.execute(sql)
        except Exception as exc:
            raise RuntimeError(f"model {layer}/{name} failed: {exc}") from exc
        for table in re.findall(r"CREATE OR REPLACE TABLE\s+([\w.]+)", sql, flags=re.I):
            built[table] = scalar(con, f"SELECT COUNT(*) FROM {table}")
    return built


def table_exists(con: duckdb.DuckDBPyConnection, qualified: str) -> bool:
    schema, table = qualified.split(".")
    return bool(
        scalar(
            con,
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = ? AND table_name = ?",
            [schema, table],
        )
    )
