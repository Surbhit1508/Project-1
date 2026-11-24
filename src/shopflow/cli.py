"""Command-line interface: ``shopflow <command>``."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from .config import Paths
from .warehouse import connect


def _cmd_generate(args: argparse.Namespace, paths: Paths) -> int:
    from .generate import GeneratorConfig, generate

    cfg = GeneratorConfig(n_customers=args.customers, seed=args.seed,
                          start=date.fromisoformat(args.start), end=date.fromisoformat(args.end),
                          dirty=not args.clean)
    s = generate(paths.landing, cfg)
    print(f"Generated {s.customers:,} customers, {s.products} products, {s.orders:,} orders "
          f"({s.order_events:,} order events in {s.order_files} files, "
          f"{s.customer_events:,} customer CDC events in {s.customer_files} files)")
    if s.injected:
        print("Injected data problems:")
        for k, v in s.injected.most_common():
            print(f"  {k:<26} {v:>6,}")
    return 0


def _cmd_run(args: argparse.Namespace, paths: Paths) -> int:
    from .pipeline import Context, build_pipeline

    pipe = build_pipeline(with_ml=not args.skip_ml, with_report=not args.skip_report)
    with connect(paths.warehouse) as con:
        ctx = Context(con, paths)
        outcomes = pipe.run(ctx, only=set(args.only) if args.only else None)
    width = max(len(o.name) for o in outcomes)
    print(f"\nRun {ctx.run_id}")
    for o in outcomes:
        line = f"  {o.name:<{width}}  {o.status:<8} {o.seconds:6.1f}s"
        print(line + (f"  {o.error}" if o.error else ""))
    return 0 if all(o.status == "success" for o in outcomes) else 1


def _cmd_dq(args: argparse.Namespace, paths: Paths) -> int:
    from .quality import CHECKS_BY_LAYER, run_checks

    with connect(paths.warehouse) as con:
        results = [r for layer in args.layers for r in run_checks(con, CHECKS_BY_LAYER[layer])]
    failed = [r for r in results if not r.passed]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if any(r.check.severity == "error" for r in failed) else 0


def _cmd_sql(args: argparse.Namespace, paths: Paths) -> int:
    import pandas as pd

    with connect(paths.warehouse) as con, pd.option_context(
        "display.max_columns", 50, "display.width", 200
    ):
        print(con.execute(args.query).df().to_string(index=False, max_rows=args.limit))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shopflow", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate", help="write synthetic source data to the landing zone")
    g.add_argument("--customers", type=int, default=4000)
    g.add_argument("--seed", type=int, default=7)
    g.add_argument("--start", default="2024-01-01")
    g.add_argument("--end", default="2025-06-30")
    g.add_argument("--clean", action="store_true", help="do not inject data-quality problems")
    g.set_defaults(fn=_cmd_generate)

    r = sub.add_parser("run", help="run the pipeline (ingest → silver → gold → ML → report)")
    r.add_argument("--skip-ml", action="store_true")
    r.add_argument("--skip-report", action="store_true")
    r.add_argument("--only", nargs="+", metavar="TASK", help="run only these tasks")
    r.set_defaults(fn=_cmd_run)

    d = sub.add_parser("dq", help="run data-quality checks against the warehouse")
    d.add_argument("layers", nargs="*", default=["silver", "gold"], choices=["silver", "gold"])
    d.set_defaults(fn=_cmd_dq)

    q = sub.add_parser("sql", help="run an ad-hoc SQL query against the warehouse")
    q.add_argument("query")
    q.add_argument("--limit", type=int, default=50)
    q.set_defaults(fn=_cmd_sql)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    return args.fn(args, Paths.from_env().ensure())


if __name__ == "__main__":
    sys.exit(main())
