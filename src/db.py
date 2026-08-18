"""Shared helpers for the pipeline stages and the dashboard.

Everything in this project runs under `irispython`, inside the IRIS process:
the loader, the analytics, and the Flask app that IRIS hosts as a WSGI
application. So `iris.sql.exec` is an in-process call -- no TCP, no
serialisation, and an aggregate over 787,060 rows crosses the boundary as a few
summary rows.

There is deliberately no off-process path. Half the pipeline could not use one
anyway (prepare() and stage 3's iris.tstart()/tcommit() have no DB-API
equivalent), and a second transport means every result shape has to be
normalised twice. One way in, one set of types out.

Row values arrive as Python `int`, `float` and `str`; SQL NULL arrives as `''`,
which is why dashboard.py coerces through num()/txt() rather than testing
`is None`. Timestamps come back as the internal integer, so callers CAST them to
VARCHAR in the query.
"""

import os
import sys

# Pin this directory on sys.path *before* importing iris. `import iris` changes
# the process working directory (under irispython, to the namespace's own
# directory), and Python's default sys.path entry for `-c`/`-m` is '', resolved
# against the cwd at import time -- so a sibling module imported after iris is not
# found (ModuleNotFoundError, several frames from the cause). db is the module
# that imports iris, so it is the right place to make the path cwd-independent.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import iris  # noqa: E402

try:
    # irisbuiltins exists only inside the IRIS process, so importing it *is* the
    # embedded test. Failing here, at import, is the point: the alternative is a
    # stage that starts cleanly and then dies on its first statement.
    from irisbuiltins import SQLError
except ImportError as exc:  # pragma: no cover -- wrong interpreter
    raise ImportError(
        "this project runs inside IRIS under Embedded Python. Use:\n"
        "  docker exec -w /src nyc-taxi-iris /usr/irissys/bin/irispython run.py"
    ) from exc

TRIP_CSV = "/data/2023_Green_Taxi_Trip_Data.csv"
ZONE_CSV = "/data/taxi_zone_lookup(in).csv"

# Column names of the trip CSV, in file order. LOAD DATA matches a target table's
# columns to the file's header *by name*, so TripRaw has to reuse these verbatim.
RAW_COLUMNS = [
    "VendorID",
    "lpep_pickup_datetime",
    "lpep_dropoff_datetime",
    "store_and_fwd_flag",
    "RatecodeID",
    "PULocationID",
    "DOLocationID",
    "passenger_count",
    "trip_distance",
    "fare_amount",
    "extra",
    "mta_tax",
    "tip_amount",
    "tolls_amount",
    "ehail_fee",
    "improvement_surcharge",
    "total_amount",
    "payment_type",
    "trip_type",
    "congestion_surcharge",
]


def exec_sql(statement, *params):
    """Run a query, returning rows as a list of lists."""
    return [list(row) for row in iris.sql.exec(statement, *params)]


def exec_columns(statement, *params):
    """Run a query, returning (column_names, rows).

    The single-trip view reads Taxi.TripRaw with SELECT *, so it needs the column
    names to label 20 CSV fields it does not hardcode.
    """
    result = iris.sql.exec(statement, *params)
    # %GetMetadata() on the underlying %SQL.StatementResult. `columns` is an
    # ObjectScript list, so it is 1-based -- GetAt(0) returns nothing rather
    # than raising, which would silently drop the first column name.
    metadata = result.ResultSet._GetMetadata()
    columns = [str(metadata.columns.GetAt(i).colName)
               for i in range(1, int(metadata.columnCount) + 1)]
    return columns, [list(row) for row in result]


def iter_rows(statement, *params):
    """Stream a query's rows one at a time instead of materialising them.

    For the push-down comparison in dashboard.py: loading 787,060 rows into a list
    first would measure the list, not the row-by-row approach. Yielding keeps that
    path's memory profile honest.
    """
    for row in iris.sql.exec(statement, *params):
        yield list(row)


def exec_dml(statement, *params):
    """Run INSERT/UPDATE/DELETE, returning the number of rows affected.

    iris.sql.exec raises SQLError with an *empty* message when a statement affects
    zero rows (SQLCODE=100), which is a normal outcome -- an empty-table DELETE, a
    rule matching nothing -- so it is translated back to 0 here; genuine errors
    always carry a message.
    """
    try:
        result = iris.sql.exec(statement, *params)
    except SQLError as exc:
        if str(exc) == "":
            return 0
        raise
    return result.ResultSet._ROWCOUNT


def prepare(statement):
    """Prepare once, execute many. Avoids re-parsing SQL per row.

    The loader inserts 787,060 rows through a prepared statement in one
    transaction; re-parsing the INSERT each time would dominate the runtime.
    """
    return iris.sql.prepare(statement)


def sql_params(values):
    """Translate Python None into SQL NULL.

    Neither iris.sql.exec nor a prepared statement's execute() accepts None -- it
    arrives as an object reference and fails validation. The empty string is what
    IRIS reads as NULL, for INTEGER, NUMERIC, VARCHAR and TIMESTAMP alike.
    """
    return tuple("" if value is None else value for value in values)


def scalar(statement, *params):
    """Run a statement expected to yield one row, one column."""
    rows = exec_sql(statement, *params)
    return rows[0][0] if rows else None


def try_exec(statement, *params):
    """Run a statement, swallowing errors. For idempotent DROPs and the like."""
    try:
        iris.sql.exec(statement, *params)
        return True
    except Exception:
        return False


# Shown in the dashboard footer. The page states where its SQL was issued from,
# because "the aggregate ran inside the database" is the claim the whole project
# is making and it should be visible on the page making it.
MODE = {"label": "Embedded Python (in-process, iris.sql.exec)"}


def log(message):
    print(message, flush=True)


def banner(title):
    log("")
    log("=" * 68)
    log(title)
    log("=" * 68)
