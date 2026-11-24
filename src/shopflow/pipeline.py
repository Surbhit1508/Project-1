"""A small DAG orchestrator for the ShopFlow pipeline.

Production would use Airflow / Dagster / Prefect; this keeps the same ideas in ~100 lines so the
project runs anywhere with zero infrastructure:

* tasks declare dependencies and are executed in topological order (cycles are rejected),
* each task can retry,
* a failure stops the run and marks every downstream task as ``skipped``,
* every task attempt is recorded in ``meta.task_runs`` (run id, status, duration, error).
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import duckdb

from . import ingest as ingest_mod
from . import quality, warehouse
from .config import Paths

log = logging.getLogger(__name__)


@dataclass
class Context:
    con: duckdb.DuckDBPyConnection
    paths: Paths
    run_id: str = field(default_factory=lambda: datetime.now().strftime("%Y%m%dT%H%M%S-")
                        + uuid.uuid4().hex[:6])
    outputs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Task:
    name: str
    fn: Callable[[Context], Any]
    deps: tuple[str, ...] = ()
    retries: int = 0


@dataclass
class TaskOutcome:
    name: str
    status: str  # success | failed | skipped
    seconds: float = 0.0
    attempts: int = 0
    error: str | None = None


class Pipeline:
    def __init__(self, tasks: list[Task]):
        self.tasks = {t.name: t for t in tasks}
        for t in tasks:
            missing = [d for d in t.deps if d not in self.tasks]
            if missing:
                raise ValueError(f"task {t.name!r} depends on unknown task(s) {missing}")
        self.order = self._toposort()

    def _toposort(self) -> list[str]:
        indegree = {n: len(t.deps) for n, t in self.tasks.items()}
        ready = [n for n, d in indegree.items() if d == 0]
        order: list[str] = []
        while ready:
            node = ready.pop(0)
            order.append(node)
            for n, t in self.tasks.items():
                if node in t.deps:
                    indegree[n] -= 1
                    if indegree[n] == 0:
                        ready.append(n)
        if len(order) != len(self.tasks):
            raise ValueError("pipeline has a dependency cycle")
        return order

    def downstream(self, name: str) -> set[str]:
        out, frontier = set(), {name}
        while frontier:
            nxt = {n for n, t in self.tasks.items() if set(t.deps) & frontier} - out
            out |= nxt
            frontier = nxt
        return out

    def run(self, ctx: Context, only: set[str] | None = None) -> list[TaskOutcome]:
        _ensure_log_table(ctx.con)
        outcomes: dict[str, TaskOutcome] = {}
        blocked: set[str] = set()
        for name in self.order:
            if only is not None and name not in only:
                continue
            if name in blocked:
                outcomes[name] = TaskOutcome(name, "skipped")
                _record(ctx, outcomes[name], None)
                continue
            task = self.tasks[name]
            log.info("▶ %s", name)
            started = datetime.now()
            t0 = time.perf_counter()
            for attempt in range(1, task.retries + 2):
                try:
                    ctx.outputs[name] = task.fn(ctx)
                    outcomes[name] = TaskOutcome(name, "success", time.perf_counter() - t0,
                                                 attempt)
                    break
                except Exception as exc:  # noqa: BLE001 - we record and re-raise below
                    log.error("✖ %s attempt %d failed: %s", name, attempt, exc)
                    outcomes[name] = TaskOutcome(name, "failed", time.perf_counter() - t0,
                                                 attempt, f"{type(exc).__name__}: {exc}")
            _record(ctx, outcomes[name], started)
            if outcomes[name].status == "failed":
                blocked |= self.downstream(name)
            else:
                log.info("✔ %s (%.1fs)", name, outcomes[name].seconds)
        return [outcomes[n] for n in self.order if n in outcomes]


def _ensure_log_table(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS meta.task_runs (
            run_id VARCHAR, task VARCHAR, status VARCHAR, attempts INTEGER,
            started_at TIMESTAMP, seconds DOUBLE, error VARCHAR
        )
        """
    )


def _record(ctx: Context, o: TaskOutcome, started: datetime | None) -> None:
    ctx.con.execute("INSERT INTO meta.task_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [ctx.run_id, o.name, o.status, o.attempts, started, round(o.seconds, 3),
                     o.error])


# ------------------------------------------------------------------------ the ShopFlow DAG
def _ingest(ctx: Context):
    return ingest_mod.ingest(ctx.con, ctx.paths.landing)


def _dq(layer: str) -> Callable[[Context], Any]:
    def run(ctx: Context):
        results = quality.run_checks(ctx.con, quality.CHECKS_BY_LAYER[layer], ctx.run_id)
        quality.enforce(results)
        return results
    return run


def _train(ctx: Context):
    from .ml import churn
    return churn.train(ctx.con, ctx.paths.artifacts)


def _score(ctx: Context):
    from .ml import churn
    return churn.score(ctx.con, ctx.paths.artifacts)


def _report(ctx: Context):
    from . import report
    return report.build(ctx.con, ctx.paths, ctx.run_id)


def build_pipeline(with_ml: bool = True, with_report: bool = True) -> Pipeline:
    tasks = [
        Task("ingest_bronze", _ingest, retries=1),
        Task("build_silver", lambda c: warehouse.run_layer(c.con, "silver"), ("ingest_bronze",)),
        Task("dq_silver", _dq("silver"), ("build_silver",)),
        Task("build_gold", lambda c: warehouse.run_layer(c.con, "gold"), ("dq_silver",)),
        Task("dq_gold", _dq("gold"), ("build_gold",)),
    ]
    if with_ml:
        tasks += [
            Task("train_churn_model", _train, ("dq_gold",)),
            Task("score_customers", _score, ("train_churn_model",)),
        ]
    if with_report:
        tasks.append(Task("build_report", _report,
                          ("score_customers",) if with_ml else ("dq_gold",)))
    return Pipeline(tasks)
