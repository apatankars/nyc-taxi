"""Command-line entry point.

    python -m taxi.cli pipeline      # everything, from empty database to indexes
    python -m taxi.cli profile       # what is wrong with the raw file
    python -m taxi.cli quality       # re-apply the rules and show the counts
    python -m taxi.cli analyze       # run every analytical workflow
    python -m taxi.cli bench         # the IRIS-side vs Python-side comparison
    python -m taxi.cli info          # connection check and row counts

The dashboard (``python -m taxi.web``) is the primary interface for reading
results; this is here so the pipeline is reproducible in one command and so the
stages can be re-run individually while iterating.
"""

import argparse
import sys
from typing import List, Optional

import pandas as pd

from . import analytics, db, load, quality, schema, transform, udf
from .config import IrisConfig, RAW_TRIPS, SCHEMA, TRIPS, ZONES


def _show(title: str, frame) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")
    with pd.option_context(
        "display.width", 200, "display.max_columns", 30, "display.max_colwidth", 45
    ):
        print(frame)


def cmd_info(_: argparse.Namespace) -> None:
    cfg = IrisConfig.from_env()
    print(f"Connected to {cfg.host}:{cfg.port} namespace {cfg.namespace}")
    print(db.server_info())
    print()
    for table in (ZONES, RAW_TRIPS, TRIPS, f"{SCHEMA}.TripReject"):
        if db.table_exists(table):
            print(f"  {table:24} {db.row_count(table):>10,} rows")
        else:
            print(f"  {table:24} {'absent':>10}")


def cmd_pipeline(args: argparse.Namespace) -> None:
    """Full rebuild. Each stage prints its own elapsed time."""
    print("Stage 1/7: schema")
    schema.create_all()

    print("\nStage 2/7: bulk load (server-side LOAD DATA)")
    load.load_all()

    print("\nStage 3/7: cast, derive and enrich")
    counts = transform.cast_and_enrich()

    print("\nStage 4/7: quality rules")
    rule_summary = quality.apply_rules()

    print("\nStage 5/7: indexes")
    schema.create_indexes()

    print("\nStage 6/7: optimiser statistics")
    schema.tune_tables()

    # Last, because it calibrates itself against the loaded and flagged trips: the
    # tariff it compiles into IRIS is fitted to whatever stage 4 considered clean.
    print("\nStage 7/7: Python UDF (fare tariff, fitted then compiled into IRIS)")
    udf.deploy()

    _show("Quality rules", rule_summary)
    _show("Issues per trip", quality.overall())
    print(
        f"\nDone. {counts['loaded']:,} trips loaded, {counts['rejected']:,} rejected."
    )
    if not args.quiet:
        print("Next: `python -m taxi.web` for the dashboard, or `python -m taxi.cli analyze`")


def cmd_profile(_: argparse.Namespace) -> None:
    _show("Raw-file data quality (computed in IRIS)", transform.profile_raw())
    rejects = transform.rejects()
    _show(
        "Rows rejected by the cast",
        rejects if not rejects.empty else "(none -- every row was castable)",
    )


def cmd_quality(args: argparse.Namespace) -> None:
    if args.derive_thresholds:
        derived = quality.derive_thresholds()
        print("Thresholds measured from the loaded data (upper 0.1% tail):")
        for key, value in derived.items():
            print(f"  {key:22} default {quality.DEFAULT_THRESHOLDS[key]:>10}  "
                  f"derived {value:>10}")
        summary = quality.apply_rules(derived)
    else:
        summary = quality.apply_rules()

    _show("Quality rules", summary)
    _show("Issues per trip", quality.overall())
    _show("Rows usable per measure (the analytics' denominators)",
          quality.measure_coverage())
    if args.sample:
        _show(f"Sample rows flagged by {args.sample}", quality.sample(args.sample))


def cmd_analyze(_: argparse.Namespace) -> None:
    _show("Headline numbers", analytics.headline_numbers())
    _show(
        "Impact of the quality workflow (unfiltered vs blunt vs selective)",
        analytics.cleaning_impact(),
    )
    _show("Rows usable per measure", quality.measure_coverage())
    _show("Busiest pickup zones", analytics.busiest_zones("pickup", limit=12))
    _show("Busiest drop-off zones", analytics.busiest_zones("dropoff", limit=12))
    _show("By pickup borough", analytics.borough_summary())
    _show("By hour of day", analytics.activity_by_hour())
    _show("By day of week", analytics.activity_by_day_of_week())
    _show("By month", analytics.activity_by_month())
    _show("Tipping by borough and payment type", analytics.tipping_by_borough())
    _show("Highest fare per mile", analytics.fare_comparison_across_zones())
    _show("Most common origin/destination pairs", analytics.common_od_pairs(limit=15))
    _show(
        "Cross-borough pairs only",
        analytics.common_od_pairs(limit=12, cross_borough_only=True),
    )
    _show("total_amount reconciliation breakdown", analytics.total_reconciliation())

    # The one workflow here whose per-row arithmetic runs inside IRIS in Python
    # rather than as SQL. Printing the fitted tariff next to the gaps it produced
    # keeps the model visible: a mean gap means nothing without the rate it is a
    # gap from.
    fitted = udf.deploy(quiet=True)
    _show(
        f"Fare vs the fitted tariff, per rate code (via the {udf.FUNCTION_NAME} "
        f"Python UDF)\n"
        f"tariff: ${fitted.base:.2f} + ${fitted.per_mile:.2f}/mile + "
        f"${fitted.per_minute:.2f}/minute, flat {fitted.flat_fares}; "
        f"fitted on {fitted.rows_fitted:,} trips "
        f"(median error ${fitted.median_error:.2f}, p95 ${fitted.p95_error:.2f})",
        udf.gap_by_rate_code(),
    )


def cmd_bench(args: argparse.Namespace) -> None:
    # Imported here so that `taxi.cli info` does not pay for it.
    from . import bench

    bench.run_all(repeat=args.repeat, ingest_rows=args.ingest_rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m taxi.cli",
        description="NYC green taxi 2023 insights, on InterSystems IRIS.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("info", help="connection check and row counts")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("pipeline", help="rebuild everything from the CSVs")
    p.add_argument("--quiet", action="store_true", help="suppress the closing hint")
    p.set_defaults(func=cmd_pipeline)

    p = sub.add_parser("profile", help="report the raw file's data-quality problems")
    p.set_defaults(func=cmd_profile)

    p = sub.add_parser("quality", help="re-apply the quality rules")
    p.add_argument(
        "--derive-thresholds",
        action="store_true",
        help="measure the outlier cutoffs from the data instead of using the defaults",
    )
    p.add_argument("--sample", metavar="RULE", help="also show rows this rule flagged")
    p.set_defaults(func=cmd_quality)

    p = sub.add_parser("analyze", help="run every analytical workflow")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("bench", help="IRIS-side vs Python-side comparison")
    p.add_argument("--repeat", type=int, default=3, help="timed runs per approach")
    p.add_argument(
        "--ingest-rows",
        type=int,
        default=50_000,
        help="row subset for the ingest comparison (0 to skip it)",
    )
    p.set_defaults(func=cmd_bench)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except Exception as exc:  # noqa: BLE001 - a CLI should not show a traceback
        print(f"\nError: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
