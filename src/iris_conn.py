"""Single place where this project connects to IRIS.

Everything else imports from here, so there is exactly one copy of the host,
port and credentials, and one place to change if the environment moves.

Two connection styles are exposed because IRIS offers two genuinely different
access paths over the same stored data:

    connect()      DB-API 2.0 — SQL. This is what you want ~95% of the time.
    native()       Native API — direct global access and ObjectScript/Python
                   class-method calls. No SQL layer involved.

Two IRIS behaviours that cost us time on this project, recorded here because this
is the module every other file imports:

1. TIMESTAMP columns come back in IRIS's internal format as soon as the query
   plan has to collate them — ORDER BY, a range WHERE, or MIN()/MAX() over an
   indexed column. A plain `SELECT pickup_ts` is fine; `SELECT MIN(pickup_ts)`
   returns '1154152267907846976'. Nothing errors, the value is simply unreadable.

       SELECT MIN(pickup_ts)              -> '1154152267907846976'
       SELECT %EXTERNAL(MIN(pickup_ts))   -> '2008-12-31 22:41:41'
       SELECT CAST(pickup_ts AS TIMESTAMP) -> datetime.datetime(...)   <- for pandas

   Rule of thumb: if a timestamp is filtered, sorted or aggregated, wrap the
   output in CAST(... AS TIMESTAMP). DATEPART()/DATEDIFF() return integers and
   are unaffected.

2. VARCHAR comparison and grouping default to SQLUPPER collation. Values are
   stored with their original case, but GROUP BY returns them upper-cased and
   `WHERE borough = 'manhattan'` matches 'Manhattan'. Use %EXACT() to group on
   the stored case — MIN(borough) does *not* work, because aggregates collate
   too.

       SELECT borough FROM Taxi.Zone GROUP BY borough                  -> 'QUEENS'
       SELECT %EXACT(borough) FROM Taxi.Zone GROUP BY %EXACT(borough)  -> 'Queens'
"""

from contextlib import contextmanager
import os

import iris
from dotenv import load_dotenv

load_dotenv()

HOST = os.getenv("IRIS_HOST", "localhost")
PORT = int(os.getenv("IRIS_PORT", "1972"))
NAMESPACE = os.getenv("IRIS_NAMESPACE", "USER")
USER = os.getenv("IRIS_USER", "_SYSTEM")
PASSWORD = os.getenv("IRIS_PASSWORD", "SYS")


@contextmanager
def connect():
    """Yield a DB-API connection, closed on exit.

        with connect() as conn:
            df = pd.read_sql("SELECT ...", conn)
    """
    conn = iris.connect(HOST, PORT, NAMESPACE, USER, PASSWORD)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def native():
    """Yield a Native API handle for globals and class methods.

        with native() as n:
            print(n.classMethodValue("%SYSTEM.Version", "GetNumber"))
    """
    conn = iris.createConnection(HOST, PORT, NAMESPACE, USER, PASSWORD)
    try:
        yield iris.createIRIS(conn)
    finally:
        conn.close()


def query(sql, params=None):
    """Run a SELECT and return (column_names, list_of_row_tuples).

    Converts IRIS DataRow objects to plain tuples. Without this, rows print as
    `<iris.dbapi.DataRow object at 0x...>`, which reads like a bug but isn't.
    """
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(sql, params or [])
        columns = [d[0] for d in cur.description]
        return columns, [tuple(row) for row in cur.fetchall()]


def execute(sql, params=None):
    """Run a statement that returns no rows (DDL, INSERT, UPDATE, DELETE).

    Deliberately separate from query(): calling fetchall() after DDL raises
    <USAGE ERROR> ("previous call of execute did not return a result set"),
    which looks like the DDL failed when it actually succeeded.
    """
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(sql, params or [])
        conn.commit()


if __name__ == "__main__":
    # Smoke test:  python src/iris_conn.py
    cols, rows = query("SELECT $ZVERSION AS version")
    print(f"Connected to {HOST}:{PORT}/{NAMESPACE}")
    print(f"{cols[0]}: {rows[0][0]}")
