"""The stretch goal: how much does it matter *where* the work happens?

Two comparisons, both answering the same question from different ends of the
pipeline.

**Aggregation.** For three real analytical questions, compute the answer three
ways:

* *IRIS-side* -- ``GROUP BY`` in the database, a few dozen rows come back.
* *Python-side* -- ``SELECT`` the raw columns for all 787k rows, aggregate the
  result in pandas on the host.
* *Embedded Python* -- the same pandas reducer, running inside the IRIS process.

The third arm separates two things the first two conflate. Python-side is slower
than IRIS-side for two reasons at once: 787k rows cross the driver, *and* pandas
has to build them into a frame. Embedded Python removes only the first --
``iris.sql.exec()`` hands the rows to pandas in-process, with no wire and no
driver -- so what remains between it and the ``GROUP BY`` is the cost of
materialising every row in Python at all. Its reducer is not a reimplementation:
``inspect.getsource`` inlines the *same function object* the host arm calls into
the ``LANGUAGE PYTHON`` body, so the two arms cannot silently drift apart.

All three produce the same numbers, which the benchmark asserts rather than
assumes; a faster wrong answer is not a result. What differs is time and how much
data crosses the driver.

**Ingest.** Load the same subset of rows three ways: server-side ``LOAD DATA``,
``executemany`` in batches, and one ``execute`` per row. The three-way split
matters because the interesting difference is not Python versus IRIS, it is
*batched* versus *not*: batching gets Python to within ~2.6x of ``LOAD DATA``,
while the row-at-a-time loop -- the obvious first implementation -- is ~36x
behind. Measuring all three is what makes the recommendation specific instead of
folklore.

The ingest test runs on a subset (``--ingest-rows``), and the row-at-a-time loop
on a smaller subset still, because it is slow enough that the full file is not
worth waiting for. Both subset sizes are stated in the output, and the projected
column extrapolates each measured rate to the whole file rather than hiding it.
"""

import csv
import inspect
import json
import re
import statistics
import textwrap
import time
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd

from . import db
from .config import IrisConfig, PROJECT_ROOT, RAW_TRIPS, SCHEMA, TRIPS
from .schema import RAW_COLUMNS, TRIP_COLUMN_MAP


# --------------------------------------------------------------------------
# Aggregation: same answer, two places
# --------------------------------------------------------------------------

class Comparison:
    """One analytical question, expressed both ways."""

    def __init__(
        self,
        name: str,
        question: str,
        iris_sql: str,
        python_sql: str,
        reduce_in_pandas: Callable[[pd.DataFrame], pd.DataFrame],
    ):
        self.name = name
        self.question = question
        # Aggregates server-side; returns one row per group.
        self.iris_sql = iris_sql
        # Returns one row per *trip*; pandas does the grouping.
        self.python_sql = python_sql
        self.reduce_in_pandas = reduce_in_pandas


def _reduce_by_borough(frame: pd.DataFrame) -> pd.DataFrame:
    grouped = (
        frame.groupby("PUBorough")
        .agg(trips=("TotalAmount", "size"), revenue=("TotalAmount", "sum"))
        .reset_index()
    )
    grouped["revenue"] = grouped["revenue"].astype(float).round(0)
    return grouped.sort_values("PUBorough").reset_index(drop=True)


def _reduce_by_hour(frame: pd.DataFrame) -> pd.DataFrame:
    grouped = (
        frame.groupby("PickupHour")
        .agg(trips=("TripDistance", "size"), avg_miles=("TripDistance", "mean"))
        .reset_index()
    )
    grouped["avg_miles"] = grouped["avg_miles"].astype(float).round(2)
    return grouped.sort_values("PickupHour").reset_index(drop=True)


def _reduce_flagged(frame: pd.DataFrame) -> pd.DataFrame:
    grouped = (
        frame.groupby("QualityIssueCount")
        .agg(trips=("QualityIssueCount", "size"))
        .reset_index()
    )
    return grouped.sort_values("QualityIssueCount").reset_index(drop=True)


def comparisons() -> List[Comparison]:
    return [
        Comparison(
            name="revenue_by_borough",
            question="Total revenue and trip count per pickup borough",
            iris_sql=f"""
                SELECT PUBorough, COUNT(*) AS trips,
                       ROUND(SUM(TotalAmount), 0) AS revenue
                FROM {TRIPS}
                GROUP BY PUBorough
                ORDER BY PUBorough
            """,
            python_sql=f"SELECT PUBorough, TotalAmount FROM {TRIPS}",
            reduce_in_pandas=_reduce_by_borough,
        ),
        Comparison(
            name="distance_by_hour",
            question="Trip count and mean distance for each hour of the day",
            iris_sql=f"""
                SELECT PickupHour, COUNT(*) AS trips,
                       ROUND(AVG(TripDistance), 2) AS avg_miles
                FROM {TRIPS}
                GROUP BY PickupHour
                ORDER BY PickupHour
            """,
            python_sql=f"SELECT PickupHour, TripDistance FROM {TRIPS}",
            reduce_in_pandas=_reduce_by_hour,
        ),
        Comparison(
            name="flagged_distribution",
            question="How many trips carry 0, 1, 2... quality issues",
            iris_sql=f"""
                SELECT QualityIssueCount, COUNT(*) AS trips
                FROM {TRIPS}
                GROUP BY QualityIssueCount
                ORDER BY QualityIssueCount
            """,
            python_sql=f"SELECT QualityIssueCount FROM {TRIPS}",
            reduce_in_pandas=_reduce_flagged,
        ),
    ]


# --------------------------------------------------------------------------
# The third arm: the same pandas reducer, inside the IRIS process
# --------------------------------------------------------------------------
#
# Embedded Python reaches IRIS through the ordinary driver here, as a
# ``LANGUAGE PYTHON`` SQL function. The alternative -- `docker exec irispython
# script.py` -- would measure a process launch as well as the work, and would tie
# the benchmark to a container name. A function is callable from the same
# connection every other query uses, and returns a small JSON string, so the only
# thing that crosses the driver is the answer.
#
# The function is created through db.exec_direct for the same reason LOAD DATA is:
# the DB-API layer reads ``{`` as the start of an ODBC escape sequence, and a
# method body is one large pair of braces.

_EMBEDDED_PREFIX = f"{SCHEMA}.BenchEmbedded_"


class EmbeddedUnavailable(RuntimeError):
    """Embedded Python could not run the reducer -- see the message for why.

    Most likely the server's Python has no pandas (``pip install --target
    /usr/irissys/mgr/python pandas``, which the Dockerfile does). Raised rather
    than swallowed so the reason reaches the output instead of a blank column.
    """


def _select_columns(sql: str) -> List[str]:
    """The output column names of a plain ``SELECT a, b FROM t``.

    Needed because ``iris.sql``'s DataFrame lower-cases its column names, while
    the reducer -- being the host's, unmodified -- refers to them as IRIS spells
    them. Read off the query rather than declared a second time on Comparison, so
    there is one statement of what the columns are.
    """
    match = re.search(r"SELECT\s+(.*?)\s+FROM\s", sql, re.IGNORECASE | re.DOTALL)
    if not match:  # pragma: no cover - the three python_sql values all match
        raise ValueError(f"cannot read a select list from: {sql.strip()[:80]}")
    return [column.strip() for column in match.group(1).split(",")]


def _embedded_ddl(comparison: Comparison) -> str:
    """Wrap the host's reducer in a LANGUAGE PYTHON function, source and all.

    Body lines sit at column zero: the reducer's own source is indented relative
    to its ``def``, and adding a second level of indentation around a function
    definition would not be valid Python.
    """
    reducer = comparison.reduce_in_pandas
    return f"""
CREATE OR REPLACE FUNCTION {_EMBEDDED_PREFIX}{comparison.name}()
  RETURNS VARCHAR(32000)
  LANGUAGE PYTHON
{{
import json
import time

import iris
import pandas as pd

{textwrap.dedent(inspect.getsource(reducer)).strip()}

fetch_started = time.perf_counter()
frame = iris.sql.exec({" ".join(comparison.python_sql.split())!r}).dataframe()
fetch_seconds = time.perf_counter() - fetch_started

frame.columns = {_select_columns(comparison.python_sql)!r}

reduce_started = time.perf_counter()
reduced = {reducer.__name__}(frame)
reduce_seconds = time.perf_counter() - reduce_started

# to_json rather than to_dict: it renders numpy scalars as JSON numbers, which
# json.dumps would refuse.
return json.dumps(
    dict(
        rows_scanned=int(len(frame)),
        fetch_seconds=round(fetch_seconds, 4),
        reduce_seconds=round(reduce_seconds, 4),
        records=json.loads(reduced.to_json(orient="records")),
    )
)
}}
"""


def create_embedded_functions(cfg: Optional[IrisConfig] = None) -> None:
    """Define one LANGUAGE PYTHON function per comparison."""
    for comparison in comparisons():
        try:
            db.exec_direct(_embedded_ddl(comparison), cfg)
        except Exception as exc:  # noqa: BLE001 - reported, not handled
            raise EmbeddedUnavailable(f"could not create the function: {exc}") from exc


def drop_embedded_functions(cfg: Optional[IrisConfig] = None) -> None:
    """Remove them again. The benchmark leaves no schema behind."""
    for comparison in comparisons():
        try:
            db.execute(f"DROP FUNCTION {_EMBEDDED_PREFIX}{comparison.name}", (), cfg)
        except Exception:  # noqa: BLE001 - nothing useful to do if it lingers
            pass


def run_embedded(
    comparison: Comparison, cfg: Optional[IrisConfig] = None
) -> Tuple[pd.DataFrame, Dict[str, object]]:
    """Call one embedded function and unpack its JSON into a frame plus timings.

    The timings inside the payload are the server's own split of fetch versus
    reduce; the wall clock the caller measures around this call is the number that
    compares to the other two arms.
    """
    payload = db.scalar(f"SELECT {_EMBEDDED_PREFIX}{comparison.name}()", (), cfg)
    if payload is None:
        raise EmbeddedUnavailable("the function returned NULL")
    try:
        result = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise EmbeddedUnavailable(f"unreadable payload: {str(payload)[:120]}") from exc
    return pd.DataFrame.from_records(result["records"]), result


def _time_it(fn: Callable[[], object], repeat: int) -> Tuple[float, object]:
    """Run fn `repeat` times, return the median elapsed seconds and last result."""
    timings: List[float] = []
    result = None
    for _ in range(max(1, repeat)):
        start = time.perf_counter()
        result = fn()
        timings.append(time.perf_counter() - start)
    return statistics.median(timings), result


def _normalise(frame: pd.DataFrame) -> pd.DataFrame:
    """Make two frames comparable regardless of column naming or numeric type."""
    out = frame.copy()
    out.columns = [str(c).lower() for c in out.columns]
    out = out.reindex(sorted(out.columns), axis=1)
    for column in out.columns:
        converted = pd.to_numeric(out[column], errors="coerce")
        if converted.notna().all():
            out[column] = converted.astype(float).round(2)
    return out.sort_values(list(out.columns)).reset_index(drop=True)


def _agrees(reference: pd.DataFrame, other: pd.DataFrame, label: str) -> bool:
    """True if two arms produced the same answer; explains itself if they did not."""
    try:
        pd.testing.assert_frame_equal(
            _normalise(reference), _normalise(other), check_dtype=False
        )
        return True
    except AssertionError as exc:
        print(f"  WARNING: {label} differs from IRIS-side -- {str(exc)[:200]}")
        return False


def compare_aggregation(
    repeat: int = 3, cfg: Optional[IrisConfig] = None
) -> pd.DataFrame:
    """Run every comparison three ways; report time, rows moved, and agreement.

    If the server's Python cannot run the reducer, the embedded columns come back
    None and the reason is printed once. The other two arms are unaffected -- a
    missing third arm should not cost the benchmark its result.
    """
    total_trips = db.row_count(TRIPS, cfg)
    records = []

    embedded_note: Optional[str] = None
    try:
        create_embedded_functions(cfg)
    except EmbeddedUnavailable as exc:
        embedded_note = str(exc)
        print(f"\nEmbedded Python arm skipped: {embedded_note}")

    for comparison in comparisons():
        print(f"\n{comparison.question}")

        iris_seconds, iris_frame = _time_it(
            lambda c=comparison: db.query_df(c.iris_sql, (), cfg), repeat
        )
        iris_rows = len(iris_frame)
        print(f"  IRIS-side       {iris_seconds:7.3f}s  {iris_rows:>9,} rows returned")

        # Split fetch from reduce on this arm too, so it lines up against the
        # split the embedded arm reports and the difference is attributable.
        fetches: List[float] = []
        reduces: List[float] = []

        def python_side(c=comparison):
            started = time.perf_counter()
            raw = db.query_df(c.python_sql, (), cfg)
            fetches.append(time.perf_counter() - started)
            started = time.perf_counter()
            reduced = c.reduce_in_pandas(raw)
            reduces.append(time.perf_counter() - started)
            return raw, reduced

        python_seconds, (raw_frame, python_frame) = _time_it(python_side, repeat)
        python_rows = len(raw_frame)
        python_fetch = statistics.median(fetches)
        python_reduce = statistics.median(reduces)
        print(
            f"  Python-side     {python_seconds:7.3f}s  {python_rows:>9,} rows returned"
            f"  ({python_fetch:.3f}s fetch + {python_reduce:.3f}s reduce)"
        )

        # A comparison is only meaningful if the arms agree.
        agrees = _agrees(iris_frame, python_frame, "Python-side")

        embedded_seconds = embedded_rows = None
        embedded_scanned = embedded_fetch = embedded_reduce = None
        if embedded_note is None:
            try:
                embedded_seconds, (embedded_frame, detail) = _time_it(
                    lambda c=comparison: run_embedded(c, cfg), repeat
                )
            except Exception as exc:  # noqa: BLE001 - any failure means no third arm
                embedded_note = str(exc)
                print(f"  Embedded Python skipped: {embedded_note}")
            else:
                embedded_rows = len(embedded_frame)
                embedded_scanned = int(detail["rows_scanned"])
                embedded_fetch = float(detail["fetch_seconds"])
                embedded_reduce = float(detail["reduce_seconds"])
                print(
                    f"  Embedded Python {embedded_seconds:7.3f}s  "
                    f"{embedded_rows:>9,} rows returned  "
                    f"({embedded_scanned:,} scanned in-process: "
                    f"{embedded_fetch:.3f}s fetch + {embedded_reduce:.3f}s reduce)"
                )
                agrees = _agrees(iris_frame, embedded_frame, "Embedded Python") and agrees

        records.append(
            {
                "comparison": comparison.name,
                "iris_seconds": round(iris_seconds, 3),
                "python_seconds": round(python_seconds, 3),
                "embedded_seconds": round(embedded_seconds, 3)
                if embedded_seconds is not None
                else None,
                "speedup": round(python_seconds / iris_seconds, 1)
                if iris_seconds > 0
                else None,
                # What moving the *same* pandas code into the server buys, with the
                # driver and the wire taken out but the row-by-row work left in.
                "embedded_speedup": round(python_seconds / embedded_seconds, 1)
                if embedded_seconds
                else None,
                "python_fetch_seconds": round(python_fetch, 4),
                "python_reduce_seconds": round(python_reduce, 4),
                "iris_rows_moved": iris_rows,
                "python_rows_moved": python_rows,
                "embedded_rows_moved": embedded_rows,
                "embedded_rows_scanned": embedded_scanned,
                "embedded_fetch_seconds": embedded_fetch,
                "embedded_reduce_seconds": embedded_reduce,
                "rows_ratio": round(python_rows / iris_rows) if iris_rows else None,
                "same_answer": agrees,
            }
        )

    drop_embedded_functions(cfg)

    frame = pd.DataFrame(records)
    frame.attrs["total_trips"] = total_trips
    frame.attrs["embedded_note"] = embedded_note
    return frame


# --------------------------------------------------------------------------
# Ingest: LOAD DATA versus a Python insert loop
# --------------------------------------------------------------------------

_BENCH_TABLE = f"{SCHEMA}.IngestBench"


def _bench_table_ddl() -> List[str]:
    cols = ",\n    ".join(f"{c} VARCHAR(64)" for c in RAW_COLUMNS)
    return [
        f"DROP TABLE IF EXISTS {_BENCH_TABLE}",
        f"CREATE TABLE {_BENCH_TABLE} (\n    {cols}\n)",
    ]


def compare_ingest(
    rows: int = 50_000,
    batch_size: int = 5_000,
    row_at_a_time_rows: int = 5_000,
    cfg: Optional[IrisConfig] = None,
) -> pd.DataFrame:
    """Load the same N rows three ways and time each.

    The subset is written to ``data/_bench_subset.csv`` so that every approach
    reads byte-identical input. Its header is copied from the source file, so the
    LOAD DATA side uses exactly the same statement shape as load.py.

    ``row_at_a_time_rows`` caps the one-insert-per-row loop, which is slow enough
    that running it over the same subset as the other two would dominate the
    benchmark's runtime for no extra information.
    """
    source = PROJECT_ROOT / "data" / "2023_Green_Taxi_Trip_Data.csv"
    subset_name = "_bench_subset.csv"
    subset_host = PROJECT_ROOT / "data" / subset_name
    subset_container = f"/data/{subset_name}"

    print(f"\nPreparing a {rows:,}-row subset for the ingest comparison")
    with source.open("r", newline="", encoding="utf-8") as src:
        reader = csv.reader(src)
        header = next(reader)
        subset: List[List[str]] = [next(reader) for _ in range(rows)]
    with subset_host.open("w", newline="", encoding="utf-8") as dst:
        writer = csv.writer(dst, quoting=csv.QUOTE_ALL)
        writer.writerow(header)
        writer.writerows(subset)

    targets = ", ".join(target for target, _ in TRIP_COLUMN_MAP)
    headers = ", ".join(h for _, h in TRIP_COLUMN_MAP)
    load_sql = (
        f"LOAD DATA FROM FILE '{subset_container}' INTO {_BENCH_TABLE} ({targets}) "
        f"VALUES ({headers}) USING " + '{"from":{"file":{"header":1}}}'
    )

    # --- server-side LOAD DATA ---
    db.execute_script(_bench_table_ddl(), cfg)
    start = time.perf_counter()
    db.exec_direct(load_sql, cfg)
    load_data_seconds = time.perf_counter() - start
    load_data_rows = db.row_count(_BENCH_TABLE, cfg)
    print(f"  LOAD DATA        {load_data_seconds:7.3f}s  {load_data_rows:>9,} rows")

    # --- Python-side executemany ---
    db.execute_script(_bench_table_ddl(), cfg)
    placeholders = ", ".join("?" for _ in RAW_COLUMNS)
    insert_sql = f"INSERT INTO {_BENCH_TABLE} ({targets}) VALUES ({placeholders})"

    start = time.perf_counter()
    with db.connect(cfg) as conn:
        cursor = conn.cursor()
        try:
            for offset in range(0, len(subset), batch_size):
                batch = [
                    # Empty CSV fields become '' from csv.reader; send NULL so the
                    # two tables are actually comparable afterwards.
                    [value if value != "" else None for value in row]
                    for row in subset[offset : offset + batch_size]
                ]
                cursor.executemany(insert_sql, batch)
        finally:
            cursor.close()
    python_seconds = time.perf_counter() - start
    python_rows = db.row_count(_BENCH_TABLE, cfg)
    print(f"  executemany      {python_seconds:7.3f}s  {python_rows:>9,} rows")

    # --- Python-side, one INSERT per row: the naive implementation ---
    single_subset = subset[:row_at_a_time_rows]
    db.execute_script(_bench_table_ddl(), cfg)
    start = time.perf_counter()
    with db.connect(cfg) as conn:
        cursor = conn.cursor()
        try:
            for row in single_subset:
                cursor.execute(
                    insert_sql, [value if value != "" else None for value in row]
                )
        finally:
            cursor.close()
    single_seconds = time.perf_counter() - start
    single_rows = db.row_count(_BENCH_TABLE, cfg)
    print(f"  one INSERT/row   {single_seconds:7.3f}s  {single_rows:>9,} rows")

    subset_host.unlink(missing_ok=True)
    db.execute(f"DROP TABLE IF EXISTS {_BENCH_TABLE}", (), cfg)

    full_file_rows = db.row_count(RAW_TRIPS, cfg) or 787_060

    def record(approach: str, n: int, seconds: float) -> Dict[str, object]:
        rate = n / seconds if seconds > 0 else 0.0
        return {
            "approach": approach,
            "rows_measured": n,
            "seconds": round(seconds, 3),
            "rows_per_second": round(rate),
            "projected_full_file_seconds": round(full_file_rows / rate, 1)
            if rate
            else None,
        }

    return pd.DataFrame(
        [
            record("IRIS LOAD DATA (server-side)", load_data_rows, load_data_seconds),
            record(
                f"Python executemany (batch {batch_size:,})", python_rows, python_seconds
            ),
            record("Python one INSERT per row", single_rows, single_seconds),
        ]
    )


def run_all(
    repeat: int = 3, ingest_rows: int = 50_000, cfg: Optional[IrisConfig] = None
) -> Dict[str, pd.DataFrame]:
    total = db.row_count(TRIPS, cfg)
    print("=" * 78)
    print(
        f"Aggregation: IRIS SQL vs host pandas vs embedded Python over {total:,} trips"
    )
    print(f"(median of {repeat} runs each)")
    print("=" * 78)
    aggregation = compare_aggregation(repeat, cfg)

    print()
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(aggregation)
    if aggregation.attrs.get("embedded_note"):
        print(f"\nEmbedded Python arm unavailable: {aggregation.attrs['embedded_note']}")

    results = {"aggregation": aggregation}

    if ingest_rows > 0:
        print()
        print("=" * 78)
        print("Ingest: server-side LOAD DATA vs a Python insert loop")
        print("=" * 78)
        ingest = compare_ingest(ingest_rows, cfg=cfg)
        print()
        with pd.option_context("display.width", 200, "display.max_columns", 20):
            print(ingest)
        print(
            f"\nMeasured on a {ingest_rows:,}-row subset; the projected column "
            "extrapolates that rate to the full file."
        )
        results["ingest"] = ingest

    return results
