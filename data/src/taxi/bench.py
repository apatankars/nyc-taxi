"""The stretch goal: how much does it matter *where* the work happens?

Two comparisons, both answering the same question from different ends of the
pipeline.

**Aggregation.** For four real analytical questions, compute the answer twice:

* *IRIS-side* -- the work happens in the database, a few dozen rows come back.
* *Python-side* -- ``SELECT`` the raw columns for every row, aggregate the result
  in pandas.

Both produce the same numbers, which the benchmark asserts rather than assumes;
a faster wrong answer is not a result. What differs is time and how much data
crosses the driver.

Three of the four express the IRIS-side arm as ``GROUP BY``. The fourth
(``fare_vs_tariff``) cannot: it is a per-row decision over rate codes rather than
an aggregate, so its IRIS-side arm calls a *Python* function that was compiled
into the server -- see udf.py. That matters because "push the work down" is
otherwise easy to read as "rewrite it as SQL", and the interesting claim is the
weaker and more useful one: the work moves to the data whether or not it can be
expressed as a query. Both arms of that comparison run the same rendered function
text, so the equality assertion is checking the plumbing rather than two
hand-written copies of one formula.

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
import statistics
import time
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd

from . import db, udf
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
        uses_udf: bool = False,
    ):
        self.name = name
        self.question = question
        # Aggregates server-side; returns one row per group.
        self.iris_sql = iris_sql
        # Returns one row per *trip*; pandas does the grouping.
        self.python_sql = python_sql
        self.reduce_in_pandas = reduce_in_pandas
        # True when the IRIS-side arm runs Python *inside* the server via a UDF
        # rather than expressing the work as SQL. Reported so the dashboard can
        # say which kind of "IRIS-side" a row is; nothing here branches on it.
        self.uses_udf = uses_udf


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


def _reduce_fare_gap(frame: pd.DataFrame) -> pd.DataFrame:
    """The tariff test, done in pandas instead of inside IRIS.

    The function is applied *per row* rather than vectorised over the columns.
    That is the point: the IRIS arm calls the same scalar function once per trip,
    so a vectorised NumPy rewrite here would be measuring a different
    computation, not a different location. Vectorising would narrow the gap the
    benchmark reports -- what it would not do is move the 702k rows back off the
    wire, which is the cost this comparison exists to show.
    """
    expected_fare = udf.python_callable(udf.tariff())

    predicted = [
        expected_fare(rate, miles, minutes)
        for rate, miles, minutes in zip(
            frame["RatecodeID"], frame["TripDistance"], frame["TripMinutes"]
        )
    ]

    # float() before subtracting: IRIS hands NUMERIC columns back as Decimal, and
    # Decimal - float raises rather than coercing.
    #
    # .round(2) for the same reason the SQL rounds -- see the note on
    # udf.GAP_BY_RATE_SQL. Without it the two arms disagree on the few trips whose
    # gap lands exactly on the threshold, because this side is float64 and the
    # server side is fixed-point decimal.
    actual = pd.to_numeric(frame["FareAmount"], errors="coerce").astype(float)
    gap = (actual - pd.Series(predicted, index=frame.index, dtype="float64")).abs().round(2)

    grouped = (
        pd.DataFrame(
            {"rate_code": pd.to_numeric(frame["RatecodeID"], errors="coerce"), "gap": gap}
        )
        .groupby("rate_code")
        .agg(
            trips=("gap", "size"),
            mean_gap=("gap", "mean"),
            off_tariff=("gap", lambda s: int((s > udf.OFF_TARIFF_DOLLARS).sum())),
        )
        .reset_index()
    )
    grouped["mean_gap"] = grouped["mean_gap"].astype(float).round(2)
    return grouped.sort_values("rate_code").reset_index(drop=True)


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
        # The one comparison whose IRIS-side arm is not SQL. The work is a per-row
        # decision over rate codes, so the server-side version is a Python UDF
        # called once per trip -- see udf.py. Both arms execute the same rendered
        # function text, which is what makes the equality assertion meaningful
        # here rather than a comparison of two hand-written formulas.
        Comparison(
            name="fare_vs_tariff",
            question=(
                "How far each metered fare sits from the tariff, per rate code "
                "(Python, not SQL)"
            ),
            iris_sql=udf.GAP_BY_RATE_SQL,
            python_sql=(
                f"SELECT RatecodeID, TripDistance, TripMinutes, FareAmount "
                f"FROM {TRIPS} WHERE {udf.POPULATION}"
            ),
            reduce_in_pandas=_reduce_fare_gap,
            uses_udf=True,
        ),
    ]


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


def compare_aggregation(
    repeat: int = 3, cfg: Optional[IrisConfig] = None
) -> pd.DataFrame:
    """Run every comparison and report time, rows transferred, and agreement."""
    total_trips = db.row_count(TRIPS, cfg)
    records = []

    # Before timing anything: the fare_vs_tariff comparison calls a Python UDF that
    # has to exist server-side, and both arms have to be running the tariff that
    # was actually compiled into it. Deploying here rather than lazily inside the
    # timed block keeps the compile out of the measurement.
    deployed = udf.deploy(cfg)

    for comparison in comparisons():
        print(f"\n{comparison.question}")

        iris_seconds, iris_frame = _time_it(
            lambda c=comparison: db.query_df(c.iris_sql, (), cfg), repeat
        )
        iris_rows = len(iris_frame)
        print(f"  IRIS-side   {iris_seconds:7.3f}s  {iris_rows:>9,} rows returned")

        def python_side(c=comparison):
            raw = db.query_df(c.python_sql, (), cfg)
            return raw, c.reduce_in_pandas(raw)

        python_seconds, (raw_frame, python_frame) = _time_it(python_side, repeat)
        python_rows = len(raw_frame)
        print(f"  Python-side {python_seconds:7.3f}s  {python_rows:>9,} rows returned")

        # The comparison is only meaningful if both approaches agree.
        try:
            pd.testing.assert_frame_equal(
                _normalise(iris_frame), _normalise(python_frame), check_dtype=False
            )
            agrees = True
        except AssertionError as exc:
            agrees = False
            print(f"  WARNING: results differ -- {str(exc)[:200]}")

        records.append(
            {
                "comparison": comparison.name,
                "uses_udf": comparison.uses_udf,
                "iris_seconds": round(iris_seconds, 3),
                "python_seconds": round(python_seconds, 3),
                "speedup": round(python_seconds / iris_seconds, 1)
                if iris_seconds > 0
                else None,
                "iris_rows_moved": iris_rows,
                "python_rows_moved": python_rows,
                "rows_ratio": round(python_rows / iris_rows) if iris_rows else None,
                "same_answer": agrees,
            }
        )

    frame = pd.DataFrame(records)
    frame.attrs["total_trips"] = total_trips
    # Carried alongside the timings so callers can report the tariff the UDF arm
    # was measured against without recalibrating to find out.
    frame.attrs["tariff"] = deployed
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
    print(f"Aggregation: IRIS-side vs Python-side over {total:,} trips")
    print(f"(median of {repeat} runs each)")
    print("=" * 78)
    aggregation = compare_aggregation(repeat, cfg)

    print()
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(aggregation)

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
