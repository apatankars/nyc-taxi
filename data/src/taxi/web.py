"""A small HTTP layer over the analytics, and the dashboard it serves.

This is deliberately thin. Every endpoint does three things: call a function in
``analytics`` or ``quality``, turn the resulting DataFrame into JSON, and return
it. There is no SQL here and no business logic -- if an endpoint needs a number
the database does not already produce, the fix belongs in analytics.py, not here.

Two design notes:

**Results are cached in-process.** After the pipeline has run, ``Taxi.Trip`` does
not change, so every query is a pure function of its arguments. Caching them
means the second click on a tab is instant, which matters when the interface is
being driven live in front of an audience. ``/api/cache/clear`` exists for when
the pipeline is re-run behind the server's back.

**JSON conversion is explicit.** IRIS returns ``Decimal`` for NUMERIC columns and
``datetime.date`` for DATE, neither of which the stdlib JSON encoder handles, and
pandas returns ``NaN`` where the SQL said NULL. ``_records`` normalises all three
rather than leaving it to chance.

Run it with::

    python -m taxi.web            # http://127.0.0.1:8000
    python -m taxi.web --port 9000
"""

import argparse
import math
import threading
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

import pandas as pd
from flask import Flask, jsonify, request, send_from_directory

from . import analytics, db, quality
from .config import IrisConfig, RAW_TRIPS, SCHEMA, TRIPS, ZONES

STATIC_DIR = "static"

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")

# Flask sorts JSON keys alphabetically by default. The page builds its tables from
# the key order of the first record, and the column order the queries return is
# deliberate (measure before meaning, all_rows before selective), so keep it.
app.json.sort_keys = False

# The cache is keyed by (endpoint, sorted params) and guarded by a lock, because
# Flask's dev server is threaded and two tabs loading at once would otherwise
# race on first population.
_cache: Dict[Any, Any] = {}
_cache_lock = threading.Lock()


def _cached(key: Any, produce: Callable[[], Any]) -> Any:
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    value = produce()
    with _cache_lock:
        _cache[key] = value
    return value


# --------------------------------------------------------------------------
# JSON conversion
# --------------------------------------------------------------------------

def _clean(value: Any) -> Any:
    """One cell, in a form json.dumps accepts."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float):
        # NaN and +/-inf are not valid JSON; NULL is what they came from.
        return None if math.isnan(value) or math.isinf(value) else value
    if hasattr(value, "item"):  # numpy scalar
        return _clean(value.item())
    return value


def _records(frame: pd.DataFrame) -> List[Dict[str, Any]]:
    """A DataFrame as a list of JSON-safe dicts, column order preserved."""
    columns = list(frame.columns)
    return [
        {column: _clean(row[i]) for i, column in enumerate(columns)}
        for row in frame.itertuples(index=False, name=None)
    ]


def _series(series: pd.Series) -> Dict[str, Any]:
    return {str(k): _clean(v) for k, v in series.items()}


def _flag(name: str, default: bool = True) -> bool:
    """Read a query-string boolean. Absent means the default."""
    raw = request.args.get(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int, maximum: int = 500) -> int:
    try:
        return max(1, min(maximum, int(request.args.get(name, default))))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

@app.get("/api/health")
def api_health():
    """Connection check and row counts -- what `cli.py info` prints, as JSON.

    Not cached: this is the endpoint you reload to find out whether the database
    is actually up.
    """
    cfg = IrisConfig.from_env()
    try:
        version = db.server_info(cfg)
    except Exception as exc:  # noqa: BLE001 - reported to the page, not raised
        return jsonify({"ok": False, "error": str(exc)}), 503

    tables = []
    for table in (ZONES, RAW_TRIPS, TRIPS, f"{SCHEMA}.TripReject"):
        exists = db.table_exists(table, cfg)
        tables.append(
            {
                "table": table,
                "exists": exists,
                "rows": db.row_count(table, cfg) if exists else None,
            }
        )
    return jsonify(
        {
            "ok": True,
            "version": version,
            "target": f"{cfg.host}:{cfg.port}/{cfg.namespace}",
            "tables": tables,
        }
    )


@app.get("/api/overview")
def api_overview():
    def produce():
        return {
            "headline": _series(analytics.headline_numbers()),
            "cleaning_impact": _records(analytics.cleaning_impact()),
            "issue_distribution": _records(quality.overall()),
            "boroughs": _records(analytics.borough_summary(exclude_invalid=True)),
            # The denominators, so each overview figure can state how many rows it
            # actually rests on rather than implying the whole file.
            "coverage": _records(quality.measure_coverage()),
        }

    return jsonify(_cached("overview", produce))


@app.get("/api/quality")
def api_quality():
    """Per-rule counts. `summary` reads the existing flags; it does not re-flag."""
    def produce():
        return {
            "rules": _records(quality.summary()),
            "issue_distribution": _records(quality.overall()),
            "reconciliation": _records(analytics.total_reconciliation()),
            "thresholds": {k: float(v) for k, v in quality.DEFAULT_THRESHOLDS.items()},
            # The denominators. Because the analytics filter selectively, different
            # figures rest on different row counts, and the page should say so
            # rather than leave the reader to assume one population.
            "coverage": _records(quality.measure_coverage()),
        }

    return jsonify(_cached("quality", produce))


@app.get("/api/quality/sample")
def api_quality_sample():
    rule = request.args.get("rule", "")
    limit = _int("limit", 12, maximum=100)
    try:
        frame = _cached(("sample", rule, limit), lambda: quality.sample(rule, limit))
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify({"rule": rule, "rows": _records(frame)})


@app.get("/api/rows")
def api_rows():
    """A slice of Taxi.Trip itself, for reading rather than aggregating.

    The row viewer on the dashboard is the one place the browser is handed trip
    rows rather than group counts, so the limit is capped hard: this is for
    inspecting a handful of flagged rows, not for exporting the table.
    """
    selector = request.args.get("rule", quality.BROWSE_FLAGGED)
    limit = _int("limit", 25, maximum=200)

    def produce():
        return {
            "rule": selector,
            "matching": quality.browse_count(selector),
            "rows": _records(quality.browse(selector, limit)),
            # Sent with the rows so the picker can be built without a second
            # request. Descriptions are rendered, not raw: the raw ones still
            # carry their {max_mph}-style placeholders. Which cell to shade is
            # not here -- it is per row, in each row's `Tested` field.
            "rules": [
                {
                    "name": r.name,
                    "severity": r.severity,
                    "description": quality.rendered_description(r),
                }
                for r in quality.RULES
            ],
        }

    try:
        return jsonify(_cached(("rows", selector, limit), produce))
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 404


@app.get("/api/zones")
def api_zones():
    clean = _flag("clean")
    limit = _int("limit", 12, maximum=50)

    def produce():
        return {
            "exclude_invalid": clean,
            "pickup": _records(analytics.busiest_zones("pickup", limit, clean)),
            "dropoff": _records(analytics.busiest_zones("dropoff", limit, clean)),
            "boroughs": _records(analytics.borough_summary(clean)),
            "fare_per_mile": _records(
                analytics.fare_comparison_across_zones(
                    limit=limit, exclude_invalid=clean
                )
            ),
        }

    return jsonify(_cached(("zones", clean, limit), produce))


@app.get("/api/time")
def api_time():
    clean = _flag("clean")

    def produce():
        heatmap = analytics.hourly_heatmap(clean)
        return {
            "exclude_invalid": clean,
            "hour": _records(analytics.activity_by_hour(clean)),
            "day_of_week": _records(analytics.activity_by_day_of_week(clean)),
            "month": _records(analytics.activity_by_month(clean)),
            "heatmap": {
                "days": [str(d) for d in heatmap.index],
                "hours": [int(h) for h in heatmap.columns],
                "matrix": [[int(v) for v in row] for row in heatmap.to_numpy()],
            },
        }

    return jsonify(_cached(("time", clean), produce))


@app.get("/api/pairs")
def api_pairs():
    clean = _flag("clean")
    cross = _flag("cross", default=False)
    limit = _int("limit", 15, maximum=50)

    def produce():
        return {
            "exclude_invalid": clean,
            "cross_borough_only": cross,
            "pairs": _records(analytics.common_od_pairs(limit, cross, clean)),
        }

    return jsonify(_cached(("pairs", clean, cross, limit), produce))


@app.get("/api/tipping")
def api_tipping():
    def produce():
        return {"tipping": _records(analytics.tipping_by_borough())}

    return jsonify(_cached("tipping", produce))


@app.post("/api/bench")
def api_bench():
    """Run the stretch-goal comparison on demand.

    POST rather than GET because it is not a cheap read -- the Python-side arm
    pulls every trip across the driver, three times over. Cached once run, so the
    button can be pressed again during a demo without paying for it twice.
    """
    repeat = _int("repeat", 3, maximum=10)
    ingest_rows = _int("ingest_rows", 25_000, maximum=200_000)

    def produce():
        # Imported lazily; nothing else in this module needs either of them.
        from . import bench, udf

        aggregation = bench.compare_aggregation(repeat=repeat)
        questions = {c.name: c.question for c in bench.comparisons()}
        agg_records = _records(aggregation)
        for record in agg_records:
            record["question"] = questions.get(record["comparison"], "")

        # compare_aggregation deployed the function before timing anything and
        # left the tariff it fitted on the frame, so this reports the model the
        # numbers above were actually measured against rather than a fresh fit
        # that might differ.
        fitted = aggregation.attrs.get("tariff")
        return {
            "total_trips": int(aggregation.attrs.get("total_trips", 0)),
            "repeat": repeat,
            "aggregation": agg_records,
            "udf": {
                "function": udf.FUNCTION_NAME,
                "tariff": fitted.as_dict() if fitted is not None else None,
                # The statement that deployed it, verbatim -- body and all. Sent
                # so the page can show the code that ran inside IRIS instead of
                # asking the reader to take "a Python UDF" on trust. The whole
                # statement rather than just the body, because the CREATE wrapper
                # is the part that makes it server-side.
                "create_sql": udf.create_sql(fitted) if fitted is not None else "",
                "query": udf.GAP_BY_RATE_SQL,
                "off_tariff_dollars": udf.OFF_TARIFF_DOLLARS,
                "gap_by_rate_code": _records(udf.gap_by_rate_code()),
            },
            "ingest": _records(bench.compare_ingest(rows=ingest_rows)),
            "ingest_rows": ingest_rows,
        }

    return jsonify(_cached(("bench", repeat, ingest_rows), produce))


@app.post("/api/cache/clear")
def api_cache_clear():
    with _cache_lock:
        dropped = len(_cache)
        _cache.clear()
    return jsonify({"cleared": dropped})


@app.errorhandler(500)
def api_error(exc):  # pragma: no cover - shape of the response, not the logic
    return jsonify({"error": str(getattr(exc, "original_exception", exc))}), 500


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m taxi.web",
        description="Serve the NYC green taxi dashboard.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--debug", action="store_true", help="reload on file change")
    args = parser.parse_args(argv)

    print(f"Dashboard on http://{args.host}:{args.port}")
    print("The pipeline must have been run first: python -m taxi.cli pipeline")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
