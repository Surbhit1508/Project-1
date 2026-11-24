from __future__ import annotations

import pytest

from shopflow import quality
from shopflow.pipeline import Context, Pipeline, Task
from shopflow.warehouse import connect


@pytest.fixture()
def mem():
    con = connect(":memory:")
    con.execute("CREATE TABLE silver.t AS SELECT * FROM (VALUES (1, 'a'), (2, 'b'), (2, 'c')) v(id, s)")
    return con


def test_failing_error_check_blocks(mem):
    results = quality.run_checks(mem, [quality.unique("silver", "silver.t", "id")], "r1")
    assert results[0].failures == 1 and not results[0].passed
    with pytest.raises(quality.DataQualityError):
        quality.enforce(results)


def test_warn_check_is_recorded_but_does_not_block(mem):
    check = quality.accepted_values("silver", "silver.t", "s", ["a", "b"], severity="warn")
    results = quality.run_checks(mem, [check], "r2")
    quality.enforce(results)  # does not raise
    assert mem.execute("SELECT passed FROM meta.dq_results WHERE run_id = 'r2'").fetchall() == \
        [(False,)]


def test_tolerance(mem):
    check = quality.unique("silver", "silver.t", "id", max_failures=1)
    assert quality.run_checks(mem, [check])[0].passed


def test_all_checks_pass_on_the_real_pipeline(built, con):
    failed = con.execute(
        "SELECT check_name FROM meta.dq_results WHERE run_id = ? AND NOT passed "
        "AND severity = 'error'", [built[3].run_id]).fetchall()
    assert failed == []


def _ctx(tmp_path):
    from shopflow.config import Paths
    return Context(connect(":memory:"), Paths(tmp_path))


def test_dag_order_and_cycle_detection():
    p = Pipeline([Task("c", lambda c: None, ("b",)), Task("a", lambda c: None),
                  Task("b", lambda c: None, ("a",))])
    assert p.order == ["a", "b", "c"]
    with pytest.raises(ValueError, match="cycle"):
        Pipeline([Task("a", lambda c: None, ("b",)), Task("b", lambda c: None, ("a",))])
    with pytest.raises(ValueError, match="unknown"):
        Pipeline([Task("a", lambda c: None, ("zzz",))])


def test_failure_skips_downstream_and_retries(tmp_path):
    calls = {"flaky": 0}

    def flaky(ctx):
        calls["flaky"] += 1
        if calls["flaky"] < 2:
            raise RuntimeError("transient")
        return "ok"

    def boom(ctx):
        raise RuntimeError("bad data")

    p = Pipeline([
        Task("flaky", flaky, retries=2),
        Task("boom", boom, ("flaky",)),
        Task("after_boom", lambda c: None, ("boom",)),
        Task("independent", lambda c: None, ("flaky",)),
    ])
    ctx = _ctx(tmp_path)
    status = {o.name: (o.status, o.attempts) for o in p.run(ctx)}
    assert status["flaky"] == ("success", 2)
    assert status["boom"][0] == "failed"
    assert status["after_boom"][0] == "skipped"
    assert status["independent"][0] == "success"
    logged = ctx.con.execute("SELECT COUNT(*) FROM meta.task_runs").fetchall()[0][0]
    assert logged == 4
