"""Bronze layer: incremental, idempotent ingestion of landing files into DuckDB.

Design choices
--------------
* **Append-only, schema-on-read.** Bronze stores every field as text (items as raw JSON) plus
  lineage columns (``_source_file``, ``_batch_id``, ``_ingested_at``). Nothing is lost if an
  upstream system sends garbage; typing and validation happen in silver.
* **Explicit schema contract.** Columns are declared up front instead of inferred, so a new or
  renamed upstream field cannot silently change the warehouse.
* **Manifest-based incrementality.** ``meta.ingested_files`` records ``(path, md5)`` of every
  file loaded. Re-running ingests nothing new; a file whose content changed is re-ingested
  (silver de-duplicates by business key, so this is safe).
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

import duckdb

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Source:
    name: str
    pattern: str
    table: str
    columns: dict[str, str]
    fmt: str  # "jsonl" | "csv"


SOURCES: tuple[Source, ...] = (
    Source(
        name="products",
        pattern="products/*.csv",
        table="bronze.products",
        columns={c: "VARCHAR" for c in
                 ["product_id", "product_name", "category", "unit_price", "unit_cost"]},
        fmt="csv",
    ),
    Source(
        name="customers",
        pattern="customers/*.csv",
        table="bronze.customers",
        columns={c: "VARCHAR" for c in
                 ["customer_id", "first_name", "last_name", "email", "country",
                  "acquisition_channel", "birth_year", "signup_date", "loyalty_tier",
                  "updated_at"]},
        fmt="csv",
    ),
    Source(
        name="orders",
        pattern="orders/*.jsonl",
        table="bronze.orders",
        columns={**{c: "VARCHAR" for c in
                    ["order_id", "customer_id", "order_ts", "updated_at", "status", "channel",
                     "payment_method", "discount_code", "currency", "shipping_fee"]},
                 "items": "JSON"},
        fmt="jsonl",
    ),
)

LINEAGE_DDL = "_source_file VARCHAR, _batch_id VARCHAR, _ingested_at TIMESTAMP"


@dataclass
class IngestStats:
    source: str
    files_seen: int
    files_loaded: int
    rows_loaded: int


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sql_list(paths: list[Path]) -> str:
    return "[" + ", ".join("'" + str(p).replace("'", "''") + "'" for p in paths) + "]"


def _ensure_tables(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS meta.ingested_files (
            source VARCHAR, file_path VARCHAR, md5 VARCHAR, row_count BIGINT,
            batch_id VARCHAR, ingested_at TIMESTAMP,
            PRIMARY KEY (file_path, md5)
        )
        """
    )
    for src in SOURCES:
        cols = ", ".join(f"{c} {t}" for c, t in src.columns.items())
        con.execute(f"CREATE TABLE IF NOT EXISTS {src.table} ({cols}, {LINEAGE_DDL})")


def _reader(src: Source, files: list[Path]) -> str:
    cols = ", ".join(src.columns)
    if src.fmt == "jsonl":
        spec = "{" + ", ".join(f"'{c}': '{t}'" for c, t in src.columns.items()) + "}"
        fn = (f"read_json({_sql_list(files)}, format='newline_delimited', columns={spec}, "
              f"filename=true)")
    else:
        fn = (f"read_csv({_sql_list(files)}, header=true, all_varchar=true, filename=true, "
              f"union_by_name=true)")
    return f"SELECT {cols}, filename AS _source_file FROM {fn}"


def ingest(con: duckdb.DuckDBPyConnection, landing: Path) -> list[IngestStats]:
    """Load every not-yet-seen landing file into bronze. Safe to call repeatedly."""
    _ensure_tables(con)
    batch_id = uuid.uuid4().hex[:12]
    seen = {(p, m) for p, m in con.execute(
        "SELECT file_path, md5 FROM meta.ingested_files").fetchall()}
    stats = []
    for src in SOURCES:
        candidates = sorted(landing.glob(src.pattern))
        new = [(f, _md5(f)) for f in candidates]
        new = [(f, m) for f, m in new if (str(f), m) not in seen]
        rows = 0
        if new:
            files = [f for f, _ in new]
            con.execute("BEGIN TRANSACTION")
            try:
                con.execute(
                    f"INSERT INTO {src.table} "
                    f"SELECT *, ? AS _batch_id, current_timestamp AS _ingested_at "
                    f"FROM ({_reader(src, files)})",
                    [batch_id],
                )
                counts = dict(con.execute(
                    f"SELECT _source_file, COUNT(*) FROM {src.table} WHERE _batch_id = ? "
                    f"GROUP BY 1", [batch_id]).fetchall())
                con.executemany(
                    "INSERT OR REPLACE INTO meta.ingested_files "
                    "VALUES (?, ?, ?, ?, ?, current_timestamp)",
                    [[src.name, str(f), m, counts.get(str(f), 0), batch_id] for f, m in new],
                )
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
            rows = sum(counts.values())
        log.info("  %-9s %4d files seen, %4d new, %7d rows", src.name, len(candidates),
                 len(new), rows)
        stats.append(IngestStats(src.name, len(candidates), len(new), rows))
    return stats
