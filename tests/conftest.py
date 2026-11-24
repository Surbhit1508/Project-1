from __future__ import annotations

from datetime import date

import pytest

from shopflow.config import Paths
from shopflow.generate import GeneratorConfig, generate
from shopflow.pipeline import Context, build_pipeline
from shopflow.warehouse import connect

SMALL = GeneratorConfig(n_customers=600, n_products=60, start=date(2024, 1, 1),
                        end=date(2024, 12, 31), seed=11)


@pytest.fixture(scope="session")
def built(tmp_path_factory):
    """A small end-to-end run of the whole pipeline (generate → … → report)."""
    paths = Paths(tmp_path_factory.mktemp("shopflow")).ensure()
    summary = generate(paths.landing, SMALL)
    con = connect(paths.warehouse)
    ctx = Context(con, paths)
    outcomes = build_pipeline().run(ctx)
    assert [o.status for o in outcomes] == ["success"] * len(outcomes), outcomes
    yield paths, con, summary, ctx
    con.close()


@pytest.fixture()
def con(built):
    return built[1]
