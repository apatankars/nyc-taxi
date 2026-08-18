"""DDL for the Taxi schema, plus the one place that knows the column order.

Four tables, and the reasoning behind them is not all visible in the DDL:

*   ``trip_id`` is the line number of the row in the source CSV (the header is
    line 1, so the first trip is line 2).  Using the source line instead of a
    system-generated key buys two things: any row can be traced back to the
    exact text it came from, and ``Taxi.Trip.trip_id`` and
    ``Taxi.TripReject.line_no`` share a single numbering space -- so the two
    tables together must cover 2..N with no gaps and no overlaps.  That is the
    completeness check, and it is only possible because both tables exist.

*   ``ehail_fee`` is deliberately absent.  It is empty on all 787,060 rows, so
    carrying it would add a column that can only ever be NULL.

*   ``duration_min`` and ``implied_mph`` are derived, computed once in Python at
    load time and stored.  The alternative is an expression in every query
    (``DATEDIFF('second', pickup_ts, dropoff_ts)``), but a dozen quality rules
    and most of the analytics need them, so paying once beats recomputing.  The
    cost is that they can drift from their inputs if anyone UPDATEs a timestamp;
    nothing in this project does.

*   ``quality_status`` and ``flag_count`` duplicate information that is fully
    derivable from ``Taxi.TripFlag``.  That redundancy is intentional: it turns
    the commonest filter -- "give me the analysable rows" -- into a
    bitmap-indexed column scan instead of a join.

Indexes are created *after* bulk loading, not with the tables, because
maintaining them per-row during a 787k-row insert is markedly slower than
building them once at the end.

Every VARCHAR that holds a label or a code is declared ``COLLATE EXACT``, and
that is not cosmetic.  IRIS collates VARCHAR with SQLUPPER by default, so a
plain ``SELECT borough FROM Taxi.Zone GROUP BY borough`` returns 'MANHATTAN',
not 'Manhattan' -- DISTINCT, MIN and MAX do the same, and ``WHERE borough =
'MANHATTAN'`` matches 'Manhattan'.  The alternative to COLLATE EXACT is
wrapping every grouped label in ``%EXACT(...)`` forever and remembering to do
it.  Declaring it once here is why the analytics queries can group on plain
column names.  The trade-off is that comparisons become case-sensitive, which
for values written by this loader out of a controlled lookup is what we want.
"""

from iris_conn import connect, execute, query

# ---------------------------------------------------------------------------
# Column order.  The loader builds its INSERT from this list, so the tuple it
# assembles per row and the DDL below cannot drift apart.
# ---------------------------------------------------------------------------

TRIP_COLUMNS = [
    "trip_id",
    "vendor_id",
    "pickup_ts",
    "dropoff_ts",
    "store_and_fwd_flag",
    "ratecode_id",
    "pu_location",
    "do_location",
    "passenger_count",
    "trip_distance",
    "fare_amount",
    "extra",
    "mta_tax",
    "tip_amount",
    "tolls_amount",
    "improvement_surcharge",
    "total_amount",
    "payment_type",
    "trip_type",
    "congestion_surcharge",
    "duration_min",
    "implied_mph",
    "quality_status",
    "flag_count",
]

FLAG_COLUMNS = ["trip_id", "flag_code", "severity", "detail"]
REJECT_COLUMNS = ["line_no", "raw_line", "error"]
ZONE_COLUMNS = ["location_id", "borough", "zone", "service_zone"]


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

TABLES = {
    # 265 rows.  Small enough that the join to it is free, which is why the
    # borough/zone strings are NOT denormalised into Taxi.Trip.
    "Taxi.Zone": """
        CREATE TABLE Taxi.Zone (
            location_id  INTEGER NOT NULL PRIMARY KEY,
            borough      VARCHAR(50) COLLATE EXACT,
            zone         VARCHAR(100) COLLATE EXACT,
            service_zone VARCHAR(50) COLLATE EXACT
        )
    """,
    "Taxi.Trip": """
        CREATE TABLE Taxi.Trip (
            trip_id               INTEGER NOT NULL PRIMARY KEY,
            vendor_id             INTEGER,
            pickup_ts             TIMESTAMP,
            dropoff_ts            TIMESTAMP,
            store_and_fwd_flag    VARCHAR(1),
            ratecode_id           INTEGER,
            pu_location           INTEGER,
            do_location           INTEGER,
            passenger_count       INTEGER,
            trip_distance         DOUBLE,
            fare_amount           DOUBLE,
            extra                 DOUBLE,
            mta_tax               DOUBLE,
            tip_amount            DOUBLE,
            tolls_amount          DOUBLE,
            improvement_surcharge DOUBLE,
            total_amount          DOUBLE,
            payment_type          INTEGER,
            trip_type             INTEGER,
            congestion_surcharge  DOUBLE,
            duration_min          DOUBLE,
            implied_mph           DOUBLE,
            quality_status        VARCHAR(8) COLLATE EXACT,
            flag_count            INTEGER
        )
    """,
    # One row per (trip, rule that fired).  A clean trip has no rows here at
    # all -- absence is the "nothing wrong" signal.  This shape is what lets a
    # rule be retuned or added without rewriting a column across 787k rows.
    "Taxi.TripFlag": """
        CREATE TABLE Taxi.TripFlag (
            trip_id   INTEGER NOT NULL,
            flag_code VARCHAR(32) NOT NULL COLLATE EXACT,
            severity  VARCHAR(8) NOT NULL COLLATE EXACT,
            detail    VARCHAR(120) COLLATE EXACT
        )
    """,
    # Quarantine.  A CSV line that will not even split into the expected number
    # of fields cannot go into Taxi.Trip, because it has the wrong shape rather
    # than merely bad values.  Recording it beats crashing or skipping silently.
    "Taxi.TripReject": """
        CREATE TABLE Taxi.TripReject (
            line_no  INTEGER NOT NULL PRIMARY KEY,
            raw_line VARCHAR(4000),
            error    VARCHAR(200)
        )
    """,
}


# ---------------------------------------------------------------------------
# Indexes -- built after loading.
#
# Bitmap indexes suit low-cardinality columns: quality_status has 3 distinct
# values, pu_location 265, payment_type 6, flag_code around 20.  IRIS can
# combine bitmaps cheaply with AND/OR, which is exactly what the per-question
# exclusion queries do.
# ---------------------------------------------------------------------------

INDEXES = [
    ("tripStatusIdx", "CREATE BITMAP INDEX tripStatusIdx ON Taxi.Trip(quality_status)"),
    ("tripPuIdx", "CREATE BITMAP INDEX tripPuIdx ON Taxi.Trip(pu_location)"),
    ("tripDoIdx", "CREATE BITMAP INDEX tripDoIdx ON Taxi.Trip(do_location)"),
    ("tripPayIdx", "CREATE BITMAP INDEX tripPayIdx ON Taxi.Trip(payment_type)"),
    ("flagCodeIdx", "CREATE BITMAP INDEX flagCodeIdx ON Taxi.TripFlag(flag_code)"),
    ("flagSevIdx", "CREATE BITMAP INDEX flagSevIdx ON Taxi.TripFlag(severity)"),
    # Not a bitmap: trip_id is high-cardinality, and this index serves the
    # "show me everything wrong with trip N" lookup.
    ("flagTripIdx", "CREATE INDEX flagTripIdx ON Taxi.TripFlag(trip_id)"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def insert_sql(table, columns):
    """Build a parameterised INSERT from a column list.

    Placeholders are always '?', never string interpolation of values.
    """
    placeholders = ",".join("?" * len(columns))
    return f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})"


def table_exists(table):
    schema, name = table.split(".")
    _, rows = query(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?",
        [schema, name],
    )
    return rows[0][0] > 0


def row_count(table):
    _, rows = query(f"SELECT COUNT(*) FROM {table}")
    return rows[0][0]


def drop_tables(tables=None):
    """Drop tables (and their indexes and views, which go with them)."""
    for table in reversed(list(tables or TABLES)):
        try:
            execute(f"DROP TABLE {table}")
            print(f"  dropped {table}")
        except Exception as exc:
            if "does not exist" not in str(exc):
                raise
            print(f"  {table} did not exist")


def create_tables(tables=None):
    for table, ddl in (tables or TABLES).items():
        if table_exists(table):
            print(f"  {table} already exists, left alone")
            continue
        execute(ddl)
        print(f"  created {table}")


def create_indexes():
    """Create the indexes, skipping any that already exist."""
    with connect() as conn:
        cur = conn.cursor()
        for name, ddl in INDEXES:
            try:
                cur.execute(ddl)
                conn.commit()
                print(f"  created index {name}")
            except Exception as exc:
                msg = str(exc)
                if "already" in msg.lower() or "duplicate" in msg.lower():
                    print(f"  index {name} already exists")
                else:
                    raise


if __name__ == "__main__":
    import sys

    if "--reset" in sys.argv:
        print("Dropping Taxi tables:")
        drop_tables()
    print("Creating Taxi tables:")
    create_tables()
    print("\nRow counts:")
    for table in TABLES:
        print(f"  {table:20s} {row_count(table):>10,}")
