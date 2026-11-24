from __future__ import annotations

import hashlib
import json
from datetime import date

from shopflow.generate import GeneratorConfig, generate
from shopflow.ingest import ingest
from shopflow.warehouse import connect, scalar

TINY = GeneratorConfig(n_customers=80, n_products=20, start=date(2024, 1, 1),
                       end=date(2024, 3, 31), seed=3)


def _digest(folder):
    h = hashlib.md5()
    for f in sorted(folder.rglob("*")):
        if f.is_file():
            h.update(f.relative_to(folder).as_posix().encode())
            h.update(f.read_bytes())
    return h.hexdigest()


def test_generation_is_deterministic(tmp_path):
    generate(tmp_path / "a", TINY)
    generate(tmp_path / "b", TINY)
    assert _digest(tmp_path / "a") == _digest(tmp_path / "b")


def test_clean_mode_injects_nothing(tmp_path):
    summary = generate(tmp_path, GeneratorConfig(**{**TINY.__dict__, "dirty": False}))
    assert set(summary.injected) <= {"status_update_returned"}


def test_ingest_is_incremental_and_idempotent(tmp_path):
    landing = tmp_path / "landing"
    generate(landing, TINY)
    con = connect(":memory:")

    first = {s.source: s for s in ingest(con, landing)}
    assert first["orders"].files_loaded == first["orders"].files_seen > 0
    rows = scalar(con, "SELECT COUNT(*) FROM bronze.orders")

    second = {s.source: s for s in ingest(con, landing)}
    assert all(s.files_loaded == 0 for s in second.values())
    assert scalar(con, "SELECT COUNT(*) FROM bronze.orders") == rows

    # a new daily file arrives -> only that file is loaded
    new_file = landing / "orders" / "orders_2099-01-01.jsonl"
    new_file.write_text(json.dumps({"order_id": "ONEW", "customer_id": "C000001",
                                    "order_ts": "2024-03-31T10:00:00", "status": "completed",
                                    "items": []}) + "\n")
    third = {s.source: s for s in ingest(con, landing)}
    assert (third["orders"].files_loaded, third["orders"].rows_loaded) == (1, 1)

    # the same file is re-delivered with different content -> re-ingested
    new_file.write_text(new_file.read_text() * 2)
    fourth = {s.source: s for s in ingest(con, landing)}
    assert (fourth["orders"].files_loaded, fourth["orders"].rows_loaded) == (1, 2)


def test_bronze_keeps_lineage(tmp_path):
    generate(tmp_path, TINY)
    con = connect(":memory:")
    ingest(con, tmp_path)
    nulls = scalar(con, "SELECT COUNT(*) FROM bronze.orders WHERE _source_file IS NULL "
                        "OR _batch_id IS NULL OR _ingested_at IS NULL")
    assert nulls == 0
